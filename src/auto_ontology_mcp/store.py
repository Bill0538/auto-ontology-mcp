"""SQLite-backed ontology store.

Holds what Auto Ontology keeps in Postgres: the physical catalog (tables,
columns, profiles), relationships between columns, and the semantic layer on
top (Terms, ColumnAttributes, SqlAttributes), plus a glossary, verified
example queries, a query log and a change log.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS datasource (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    url TEXT NOT NULL,
    dialect TEXT,
    description TEXT,
    schemas TEXT,
    created_at TEXT NOT NULL,
    scanned_at TEXT
);

CREATE TABLE IF NOT EXISTS catalog_table (
    id TEXT PRIMARY KEY,
    datasource_id TEXT NOT NULL REFERENCES datasource(id) ON DELETE CASCADE,
    schema_name TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL,
    table_type TEXT,
    description TEXT,
    row_count INTEGER,
    pk TEXT,
    pk_inferred INTEGER NOT NULL DEFAULT 0,
    is_junction INTEGER NOT NULL DEFAULT 0,
    junction_reason TEXT,
    profiled_at TEXT,
    UNIQUE (datasource_id, schema_name, name)
);

CREATE TABLE IF NOT EXISTS catalog_column (
    id TEXT PRIMARY KEY,
    table_id TEXT NOT NULL REFERENCES catalog_table(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    data_type TEXT,
    is_nullable INTEGER,
    ordinal INTEGER,
    description TEXT,
    sample_values TEXT,
    is_unique INTEGER,
    null_fraction REAL,
    distinct_in_sample INTEGER,
    min_value TEXT,
    max_value TEXT,
    format TEXT,
    value_set_complete INTEGER NOT NULL DEFAULT 0,
    list_separator TEXT,
    UNIQUE (table_id, name)
);

CREATE TABLE IF NOT EXISTS relationship (
    id TEXT PRIMARY KEY,
    datasource_id TEXT NOT NULL REFERENCES datasource(id) ON DELETE CASCADE,
    source_column_id TEXT NOT NULL REFERENCES catalog_column(id) ON DELETE CASCADE,
    target_column_id TEXT NOT NULL REFERENCES catalog_column(id) ON DELETE CASCADE,
    origin TEXT NOT NULL,
    status TEXT NOT NULL,
    confidence REAL,
    evidence TEXT,
    cardinality TEXT,
    is_default INTEGER NOT NULL DEFAULT 1,
    role TEXT,
    description TEXT,
    updated_at TEXT,
    UNIQUE (source_column_id, target_column_id)
);

CREATE TABLE IF NOT EXISTS term (
    id TEXT PRIMARY KEY,
    datasource_id TEXT NOT NULL REFERENCES datasource(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    description TEXT,
    synonyms TEXT,
    status TEXT NOT NULL DEFAULT 'draft',
    primary_table_id TEXT REFERENCES catalog_table(id) ON DELETE SET NULL,
    note TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    certified_at TEXT,
    kind TEXT NOT NULL DEFAULT 'entity',
    body TEXT,
    spec TEXT,
    UNIQUE (datasource_id, name)
);

-- Typed links between Terms (is_a, part_of, related_to, ...), as in Auto Ontology's
-- semantic relationship types. source_id points at sources.id in the knowledge store.
CREATE TABLE IF NOT EXISTS term_relation (
    id TEXT PRIMARY KEY,
    datasource_id TEXT NOT NULL REFERENCES datasource(id) ON DELETE CASCADE,
    source_term_id TEXT NOT NULL REFERENCES term(id) ON DELETE CASCADE,
    relation TEXT NOT NULL,
    target_term_id TEXT NOT NULL REFERENCES term(id) ON DELETE CASCADE,
    description TEXT,
    source_id INTEGER,
    evidence TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (source_term_id, relation, target_term_id)
);

-- Which knowledge sources a Term's definition or body came from.
CREATE TABLE IF NOT EXISTS term_source (
    term_id TEXT NOT NULL REFERENCES term(id) ON DELETE CASCADE,
    source_id INTEGER NOT NULL,
    locator TEXT,
    evidence TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (term_id, source_id)
);

CREATE TABLE IF NOT EXISTS column_attribute (
    id TEXT PRIMARY KEY,
    term_id TEXT NOT NULL REFERENCES term(id) ON DELETE CASCADE,
    column_id TEXT NOT NULL REFERENCES catalog_column(id) ON DELETE CASCADE,
    display_name TEXT NOT NULL,
    description TEXT,
    UNIQUE (term_id, column_id)
);

CREATE TABLE IF NOT EXISTS sql_attribute (
    id TEXT PRIMARY KEY,
    term_id TEXT NOT NULL REFERENCES term(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    description TEXT,
    expression TEXT NOT NULL,
    kind TEXT NOT NULL,
    source TEXT,
    status TEXT NOT NULL DEFAULT 'draft',
    validation TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    certified_at TEXT,
    UNIQUE (term_id, name)
);

CREATE TABLE IF NOT EXISTS glossary (
    id TEXT PRIMARY KEY,
    datasource_id TEXT REFERENCES datasource(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    definition TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS glossary_scope_name
    ON glossary (COALESCE(datasource_id, ''), name);

CREATE TABLE IF NOT EXISTS example_query (
    id TEXT PRIMARY KEY,
    datasource_id TEXT NOT NULL REFERENCES datasource(id) ON DELETE CASCADE,
    question TEXT NOT NULL,
    sql TEXT NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS query_log (
    id TEXT PRIMARY KEY,
    datasource_id TEXT NOT NULL REFERENCES datasource(id) ON DELETE CASCADE,
    question TEXT,
    sql TEXT NOT NULL,
    status TEXT NOT NULL,
    row_count INTEGER,
    truncated INTEGER,
    error TEXT,
    terms TEXT,
    executed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS changelog (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT,
    action TEXT NOT NULL,
    detail TEXT
);

CREATE INDEX IF NOT EXISTS idx_column_table ON catalog_column (table_id);
CREATE INDEX IF NOT EXISTS idx_attr_term ON column_attribute (term_id);
CREATE INDEX IF NOT EXISTS idx_attr_column ON column_attribute (column_id);
CREATE INDEX IF NOT EXISTS idx_sqlattr_term ON sql_attribute (term_id);
CREATE INDEX IF NOT EXISTS idx_rel_source ON relationship (source_column_id);
CREATE INDEX IF NOT EXISTS idx_rel_target ON relationship (target_column_id);
CREATE INDEX IF NOT EXISTS idx_term_table ON term (primary_table_id);
"""

JSON_FIELDS = {"schemas", "pk", "sample_values", "evidence", "synonyms", "validation", "terms", "detail", "spec"}

# Columns added after the first release; ALTER TABLE brings older stores up to date.
_MIGRATIONS: dict[str, list[tuple[str, str]]] = {
    "catalog_column": [
        ("list_separator", "TEXT"),
    ],
    "term": [
        ("kind", "TEXT NOT NULL DEFAULT 'entity'"),
        ("body", "TEXT"),
        ("spec", "TEXT"),
    ],
}


def now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _decode(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out = dict(row)
    for key in JSON_FIELDS & out.keys():
        if isinstance(out[key], str):
            try:
                out[key] = json.loads(out[key])
            except ValueError:
                pass
    return out


def _encode(value: Any) -> Any:
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, default=str)
    if isinstance(value, bool):
        return int(value)
    return value


class Store:
    """Thin data-access layer. One short-lived connection per operation."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self.tx() as con:
            for table, columns in _MIGRATIONS.items():
                existing = {r[1] for r in con.execute(f"PRAGMA table_info({table})").fetchall()}
                if not existing:
                    continue  # fresh store: SCHEMA creates the full table
                for name, decl in columns:
                    if name not in existing:
                        con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
            con.executescript(SCHEMA)
            con.execute("PRAGMA journal_mode = WAL")

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            con = sqlite3.connect(self.path, timeout=30)
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA foreign_keys = ON")
            try:
                yield con
                con.commit()
            except BaseException:
                con.rollback()
                raise
            finally:
                con.close()

    # ------------------------------------------------------------------ generic
    def all(self, sql: str, params: tuple | list = ()) -> list[dict[str, Any]]:
        with self.tx() as con:
            return [_decode(r) for r in con.execute(sql, params).fetchall()]  # type: ignore[misc]

    def one(self, sql: str, params: tuple | list = ()) -> dict[str, Any] | None:
        with self.tx() as con:
            return _decode(con.execute(sql, params).fetchone())

    def scalar(self, sql: str, params: tuple | list = ()) -> Any:
        with self.tx() as con:
            row = con.execute(sql, params).fetchone()
            return row[0] if row else None

    def insert(self, table: str, values: dict[str, Any], con: sqlite3.Connection | None = None) -> None:
        cols = list(values)
        sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})"
        params = [_encode(values[c]) for c in cols]
        if con is not None:
            con.execute(sql, params)
        else:
            with self.tx() as c:
                c.execute(sql, params)

    def update(
        self, table: str, row_id: str, values: dict[str, Any], con: sqlite3.Connection | None = None
    ) -> None:
        if not values:
            return
        sets = ", ".join(f"{k} = ?" for k in values)
        params = [_encode(v) for v in values.values()] + [row_id]
        sql = f"UPDATE {table} SET {sets} WHERE id = ?"
        if con is not None:
            con.execute(sql, params)
        else:
            with self.tx() as c:
                c.execute(sql, params)

    def log(
        self,
        entity_type: str,
        entity_id: str | None,
        action: str,
        detail: dict[str, Any] | None = None,
        con: sqlite3.Connection | None = None,
    ) -> None:
        self.insert(
            "changelog",
            {
                "at": now(),
                "entity_type": entity_type,
                "entity_id": entity_id,
                "action": action,
                "detail": detail or {},
            },
            con,
        )

    # -------------------------------------------------------------- datasources
    def datasources(self) -> list[dict[str, Any]]:
        return self.all("SELECT * FROM datasource ORDER BY name")

    def datasource_by_name(self, name: str) -> dict[str, Any] | None:
        return self.one("SELECT * FROM datasource WHERE name = ? OR id = ?", (name, name))

    # ------------------------------------------------------------------ catalog
    def tables(self, datasource_id: str) -> list[dict[str, Any]]:
        return self.all(
            "SELECT * FROM catalog_table WHERE datasource_id = ? ORDER BY schema_name, name",
            (datasource_id,),
        )

    def table(self, table_id: str) -> dict[str, Any] | None:
        return self.one("SELECT * FROM catalog_table WHERE id = ?", (table_id,))

    def columns(self, table_id: str) -> list[dict[str, Any]]:
        return self.all(
            "SELECT * FROM catalog_column WHERE table_id = ? ORDER BY ordinal, name", (table_id,)
        )

    def columns_for_datasource(self, datasource_id: str) -> list[dict[str, Any]]:
        return self.all(
            """
            SELECT c.*, t.name AS table_name, t.schema_name AS schema_name
            FROM catalog_column c JOIN catalog_table t ON t.id = c.table_id
            WHERE t.datasource_id = ?
            ORDER BY t.schema_name, t.name, c.ordinal
            """,
            (datasource_id,),
        )

    def column(self, column_id: str) -> dict[str, Any] | None:
        return self.one(
            """
            SELECT c.*, t.name AS table_name, t.schema_name AS schema_name, t.datasource_id
            FROM catalog_column c JOIN catalog_table t ON t.id = c.table_id
            WHERE c.id = ?
            """,
            (column_id,),
        )

    # ------------------------------------------------------------ relationships
    def relationships(
        self, datasource_id: str, statuses: tuple[str, ...] = ("accepted",)
    ) -> list[dict[str, Any]]:
        marks = ", ".join("?" for _ in statuses)
        return self.all(
            f"""
            SELECT r.*,
                   sc.name AS source_column, st.id AS source_table_id,
                   st.name AS source_table, st.schema_name AS source_schema,
                   sc.is_unique AS source_is_unique, sc.data_type AS source_type,
                   tc.name AS target_column, tt.id AS target_table_id,
                   tt.name AS target_table, tt.schema_name AS target_schema,
                   tc.is_unique AS target_is_unique, tc.data_type AS target_type
            FROM relationship r
            JOIN catalog_column sc ON sc.id = r.source_column_id
            JOIN catalog_table st ON st.id = sc.table_id
            JOIN catalog_column tc ON tc.id = r.target_column_id
            JOIN catalog_table tt ON tt.id = tc.table_id
            WHERE r.datasource_id = ? AND r.status IN ({marks})
            ORDER BY st.schema_name, st.name, sc.ordinal
            """,
            (datasource_id, *statuses),
        )

    # -------------------------------------------------------------------- terms
    def terms(self, datasource_id: str) -> list[dict[str, Any]]:
        return self.all(
            """
            SELECT tm.*, t.name AS table_name, t.schema_name AS schema_name
            FROM term tm LEFT JOIN catalog_table t ON t.id = tm.primary_table_id
            WHERE tm.datasource_id = ? ORDER BY tm.name
            """,
            (datasource_id,),
        )

    def term(self, term_id: str) -> dict[str, Any] | None:
        return self.one(
            """
            SELECT tm.*, t.name AS table_name, t.schema_name AS schema_name
            FROM term tm LEFT JOIN catalog_table t ON t.id = tm.primary_table_id
            WHERE tm.id = ?
            """,
            (term_id,),
        )

    def term_attributes(self, term_id: str) -> list[dict[str, Any]]:
        return self.all(
            """
            SELECT a.*, c.name AS column_name, c.data_type, c.sample_values, c.is_unique,
                   c.format, c.null_fraction, c.min_value, c.max_value, c.value_set_complete, c.list_separator,
                   t.id AS table_id, t.name AS table_name, t.schema_name AS schema_name
            FROM column_attribute a
            JOIN catalog_column c ON c.id = a.column_id
            JOIN catalog_table t ON t.id = c.table_id
            WHERE a.term_id = ? ORDER BY c.ordinal
            """,
            (term_id,),
        )

    def attributes_for_datasource(self, datasource_id: str) -> list[dict[str, Any]]:
        return self.all(
            """
            SELECT a.*, tm.name AS term_name, tm.status AS term_status,
                   c.name AS column_name, c.data_type, c.table_id, c.sample_values,
                   c.format, c.value_set_complete, c.is_unique, c.list_separator,
                   t.name AS table_name, t.schema_name AS schema_name
            FROM column_attribute a
            JOIN term tm ON tm.id = a.term_id
            JOIN catalog_column c ON c.id = a.column_id
            JOIN catalog_table t ON t.id = c.table_id
            WHERE tm.datasource_id = ?
            """,
            (datasource_id,),
        )

    def sql_attributes(self, term_id: str) -> list[dict[str, Any]]:
        return self.all("SELECT * FROM sql_attribute WHERE term_id = ? ORDER BY name", (term_id,))

    def sql_attributes_for_datasource(self, datasource_id: str) -> list[dict[str, Any]]:
        return self.all(
            """
            SELECT s.*, tm.name AS term_name, tm.primary_table_id
            FROM sql_attribute s JOIN term tm ON tm.id = s.term_id
            WHERE tm.datasource_id = ? ORDER BY tm.name, s.name
            """,
            (datasource_id,),
        )

    def glossary(self, datasource_id: str | None) -> list[dict[str, Any]]:
        return self.all(
            "SELECT * FROM glossary WHERE datasource_id IS NULL OR datasource_id = ? ORDER BY name",
            (datasource_id,),
        )

    def examples(self, datasource_id: str) -> list[dict[str, Any]]:
        return self.all(
            "SELECT * FROM example_query WHERE datasource_id = ? ORDER BY created_at DESC",
            (datasource_id,),
        )
