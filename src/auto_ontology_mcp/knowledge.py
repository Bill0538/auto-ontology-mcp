"""The personal knowledge store: the one place the server writes.

The host model reads documents, images and conversations; this module stores
what it extracts using Auto Ontology's building blocks:

- entities: a Term backed by a table whose rows are instances (receipts,
  books, people), with typed columns and references to other entities, which
  become declared foreign keys and therefore relationships;
- concepts: a Term without a table, with a definition and a markdown body
  (a wiki page), linked to other Terms by typed relations (is_a, part_of, ...);
- sources: where each record, concept and relation came from.

Everything is written to ``knowledge.sqlite`` through parameterized statements
built here. ``run_sql`` stays read-only, and connected datasources are never
written to.
"""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator

from . import vault
from .errors import KnowledgeError
from .naming import clean_label, to_label
from .store import new_id, now
from .textindex import INDEX_TABLES, TextIndex

if TYPE_CHECKING:  # pragma: no cover
    from .service import OntologyService

KNOWLEDGE_DS = "knowledge"
SOURCES_TABLE = "sources"
MAX_RECORDS = 500
SYSTEM_COLUMNS = ("id", "_source_id", "_source_locator", "_evidence", "_note", "_created_at", "_updated_at")
RECORD_META_KEYS = {"id", "_locator", "_evidence", "_note", "_source"}
TYPES = {"text": "TEXT", "integer": "INTEGER", "number": "REAL", "date": "TEXT", "datetime": "TEXT", "boolean": "INTEGER"}
SUGGESTED_RELATIONS = (
    "is_a", "part_of", "has_part", "instance_of", "related_to", "depends_on", "causes", "used_for",
    "contrasts_with", "same_as", "example_of", "authored_by", "located_in",
)
_IDENT = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_RELATION = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
_LABEL_COLUMNS = ("name", "title", "label", "full_name", "display_name")
_TRUE = {"true", "yes", "y", "1", "t", "on", "예", "네", "참"}
_FALSE = {"false", "no", "n", "0", "f", "off", "아니오", "아니요", "거짓"}

SOURCES_DDL = f"""
CREATE TABLE IF NOT EXISTS {SOURCES_TABLE} (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    kind TEXT,
    uri TEXT,
    author TEXT,
    published_on TEXT,
    captured_at TEXT NOT NULL,
    summary TEXT,
    content TEXT,
    original TEXT
)
"""
SOURCE_FIELDS = ("title", "kind", "uri", "author", "published_on", "summary", "content", "original")
SOURCE_SPEC = {
    "table": SOURCES_TABLE,
    "columns": [
        {"name": "title", "type": "text", "display_name": "Title", "required": True},
        {"name": "kind", "type": "text", "display_name": "Kind", "description": "pdf, image, web, note, conversation, email, ..."},
        {"name": "uri", "type": "text", "display_name": "URI", "description": "File path or URL of the original"},
        {"name": "author", "type": "text", "display_name": "Author"},
        {"name": "published_on", "type": "date", "display_name": "Published On"},
        {"name": "captured_at", "type": "datetime", "display_name": "Captured At"},
        {"name": "summary", "type": "text", "display_name": "Summary"},
        {"name": "content", "type": "text", "display_name": "Content", "description": "Transcribed text or excerpt"},
        {"name": "original", "type": "text", "display_name": "Original in Obsidian",
         "description": "Vault-relative path of the full original in the Obsidian vault; link it as [[path]]"},
    ],
    "keys": [],
    "label": "title",
    "builtin": True,
}
SOURCE_DESCRIPTION = "A document, image, web page, note or conversation that knowledge was taken from."
RESERVED_TABLES = frozenset({SOURCES_TABLE, *INDEX_TABLES})


def is_builtin(term: dict[str, Any] | None) -> bool:
    """Sources, passages, the Term mirror and the search indexes: tables the server owns."""
    spec = (term or {}).get("spec") or {}
    return bool(spec.get("builtin")) or spec.get("table") in RESERVED_TABLES


def to_ident(name: str) -> str:
    """snake_case identifier from a label; '' when nothing ASCII is left."""
    words = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", (name or "").strip())
    ident = re.sub(r"[^0-9a-zA-Z]+", "_", words).strip("_").lower()
    if ident and ident[0].isdigit():
        ident = f"t_{ident}"
    return ident[:63]


def q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _parse_type(raw: str | None, ref: str | None) -> tuple[str, str | None]:
    t = (raw or "text").strip().lower()
    if t.startswith("ref:"):
        return "ref", t[4:].strip() or ref
    if t in ("ref", "reference", "link"):
        return "ref", ref
    aliases = {"string": "text", "str": "text", "int": "integer", "float": "number", "real": "number",
               "decimal": "number", "numeric": "number", "bool": "boolean", "timestamp": "datetime"}
    t = aliases.get(t, t)
    if t not in TYPES:
        raise KnowledgeError(f"Unknown column type {raw!r}. Use one of {', '.join(TYPES)} or ref:<Entity>.")
    return t, None


def coerce(value: Any, ctype: str) -> Any:
    """Normalize one value to its column type; raises KnowledgeError with a readable reason."""
    if value is None or (isinstance(value, str) and value.strip() == ""):
        return None
    if ctype == "text":
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False)
        return str(value).strip()
    if ctype in ("integer", "number"):
        if isinstance(value, bool):
            raise KnowledgeError(f"expected a {ctype}, got a boolean")
        if isinstance(value, (int, float)):
            number = value
        else:
            text = str(value).strip()
            if "%" in text:
                raise KnowledgeError(f"{text!r} is a percentage; pass the number you want stored (12 or 0.12)")
            cleaned = re.sub(r"[\s,_$€£¥₩]|(?i:krw|usd|eur|jpy|won|원)", "", text)
            try:
                number = float(cleaned)
            except ValueError as exc:
                raise KnowledgeError(f"{text!r} is not a number") from exc
        if ctype == "integer":
            if float(number) != int(float(number)):
                raise KnowledgeError(f"{value!r} is not a whole number")
            return int(float(number))
        return float(number)
    if ctype == "boolean":
        if isinstance(value, bool):
            return int(value)
        text = str(value).strip().lower()
        if text in _TRUE:
            return 1
        if text in _FALSE:
            return 0
        raise KnowledgeError(f"{value!r} is not true/false")
    if ctype == "date":
        if isinstance(value, datetime):
            return value.date().isoformat()
        if isinstance(value, date):
            return value.isoformat()
        text = str(value).strip()
        m = re.match(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", text)
        if not m:
            raise KnowledgeError(f"{text!r} is not a date; use YYYY-MM-DD")
        try:
            return date(int(m[1]), int(m[2]), int(m[3])).isoformat()
        except ValueError as exc:
            raise KnowledgeError(f"{text!r} is not a valid date") from exc
    if ctype == "datetime":
        if isinstance(value, datetime):
            return value.isoformat(timespec="seconds")
        text = str(value).strip().replace("/", "-")
        try:
            return datetime.fromisoformat(text).isoformat(timespec="seconds")
        except ValueError as exc:
            raise KnowledgeError(f"{value!r} is not a date-time; use ISO 8601 (2026-11-06T14:30)") from exc
    raise KnowledgeError(f"unsupported type {ctype}")


class Knowledge:
    def __init__(self, svc: "OntologyService") -> None:
        self.svc = svc
        self.store = svc.store
        self.path: Path = svc.settings.home / "knowledge.sqlite"
        self.index = TextIndex(self)

    # --------------------------------------------------------------- vault
    @property
    def vault_root(self) -> Path:
        """The Obsidian vault that holds originals; the export folder unless AUTO_ONTOLOGY_VAULT is set."""
        return (self.svc.settings.vault or self.svc.settings.home / "wiki" / KNOWLEDGE_DS).expanduser()

    @property
    def wiki_dir(self) -> Path:
        """Where export_wiki writes the knowledge store: the vault itself, or a subfolder of the user's own vault."""
        return self.vault_root if self.svc.settings.vault is None else self.vault_root / "auto-ontology"

    @property
    def originals_dir(self) -> Path:
        return self.wiki_dir / "originals"

    # ------------------------------------------------------------ plumbing
    @contextmanager
    def _write(self, foreign_keys: bool = True) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.path, timeout=30)
        con.row_factory = sqlite3.Row
        if foreign_keys:
            con.execute("PRAGMA foreign_keys = ON")
        try:
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    def ds(self, create: bool = True) -> dict[str, Any] | None:
        ds = self.store.datasource_by_name(KNOWLEDGE_DS)
        if ds and self.path.exists():
            self.index.ensure_schema(ds)
        if ds or not create:
            return ds
        with self._write() as con:
            con.execute(SOURCES_DDL)
        ds = {
            "id": new_id("ds"),
            "name": KNOWLEDGE_DS,
            "url": f"sqlite:///{self.path.resolve()}",
            "dialect": "sqlite",
            "description": "Personal knowledge store: entities, concepts and sources saved by the assistant.",
            "created_at": now(),
        }
        self.store.insert("datasource", ds)
        self._refresh(ds)
        self._ensure_source_term(ds)
        self.index.ensure_schema(ds)
        return self.store.datasource_by_name(KNOWLEDGE_DS)

    def _ensure_source_term(self, ds: dict[str, Any]) -> dict[str, Any]:
        return self._ensure_term(ds, "Source", SOURCE_DESCRIPTION, SOURCE_SPEC)

    def is_knowledge(self, ds: dict[str, Any]) -> bool:
        return ds.get("name") == KNOWLEDGE_DS

    def _refresh(self, ds: dict[str, Any], profile: tuple[str, ...] = ()) -> None:
        """Sync the catalog after DDL or writes, and re-profile the tables that changed."""
        scanner = self.svc.scanner
        scanner.scan(ds, profile=False, infer=False)
        scanner._refresh_cardinality_and_defaults(ds)
        tables = {t["name"]: t for t in self.store.tables(ds["id"])}
        warnings: list[str] = []
        for name in dict.fromkeys(profile):
            if name in tables:
                scanner._profile_table(ds, tables[name], warnings)

    def _find_term(self, ds: dict[str, Any], ref: str) -> dict[str, Any] | None:
        ref = (ref or "").strip()
        for t in self.store.terms(ds["id"]):
            if t["id"] == ref or t["name"].casefold() == ref.casefold():
                return t
        for t in self.store.terms(ds["id"]):
            if any(s.casefold() == ref.casefold() for s in t.get("synonyms") or []):
                return t
        return None

    def entity(self, ds: dict[str, Any], ref: str) -> dict[str, Any]:
        t = self._find_term(ds, ref)
        if t is None or not t.get("spec"):
            names = [x["name"] for x in self.store.terms(ds["id"]) if x.get("spec") and not is_builtin(x)]
            hint = f" Entities: {', '.join(names)}." if names else ""
            raise KnowledgeError(f"{ref!r} is not a knowledge entity. Create it with define_entity first.{hint}")
        if is_builtin(t):
            raise KnowledgeError(_builtin_message(t))
        return t

    def _ensure_term(self, ds: dict[str, Any], name: str, description: str, spec: dict[str, Any]) -> dict[str, Any]:
        t = self._find_term(ds, name)
        table = next((x for x in self.store.tables(ds["id"]) if x["name"] == spec["table"]), None)
        stamp = now()
        if t is None:
            tid = new_id("term")
            self.store.insert(
                "term",
                {
                    "id": tid, "datasource_id": ds["id"], "name": name, "description": description, "synonyms": [],
                    "status": "draft", "primary_table_id": table["id"] if table else None, "kind": "entity",
                    "spec": spec, "created_at": stamp, "updated_at": stamp,
                },
            )
        else:
            tid = t["id"]
            self.store.update("term", tid, {"spec": spec, "kind": "entity", "primary_table_id": table["id"] if table else None, "updated_at": stamp})
        self._sync_attributes(ds, tid, name, spec)
        return self.store.term(tid)  # type: ignore[return-value]

    def _sync_attributes(self, ds: dict[str, Any], term_id: str, term_name: str, spec: dict[str, Any]) -> None:
        table = next((x for x in self.store.tables(ds["id"]) if x["name"] == spec["table"]), None)
        if table is None:
            return
        cols = {c["name"]: c for c in self.store.columns(table["id"])}
        wanted: dict[str, tuple[str, str | None]] = {"id": (f"{term_name} ID", "Row id in the knowledge store")}
        for c in spec["columns"]:
            wanted[c["name"]] = (c.get("display_name") or to_label(c["name"]), c.get("description"))
        with self.store.tx() as con:
            con.execute("DELETE FROM column_attribute WHERE term_id = ?", (term_id,))
            for col_name, (display, desc) in wanted.items():
                col = cols.get(col_name)
                if col is None:
                    continue
                self.store.insert(
                    "column_attribute",
                    {"id": new_id("attr"), "term_id": term_id, "column_id": col["id"], "display_name": display, "description": desc},
                    con,
                )
                if desc:
                    con.execute("UPDATE catalog_column SET description = ? WHERE id = ?", (desc, col["id"]))

    # ------------------------------------------------------------ entities
    def define_entity(
        self,
        name: str,
        columns: list[dict[str, Any]] | None = None,
        description: str | None = None,
        table: str | None = None,
        keys: list[str] | None = None,
        synonyms: list[str] | None = None,
        delete: bool = False,
        force: bool = False,
    ) -> dict[str, Any]:
        ds = self.ds()
        label = clean_label(name)
        if not label:
            raise KnowledgeError("Give the entity a name, e.g. 'Receipt'.")
        existing = self._find_term(ds, label)
        if delete:
            return self._delete_entity(ds, existing, label, force)
        if is_builtin(existing):
            raise KnowledgeError(_builtin_message(existing))

        spec = dict(existing["spec"]) if existing and existing.get("spec") else None
        if spec:
            table_name = spec["table"]
        else:
            table_name = to_ident(table or label)
            if table and to_ident(table) != table:
                raise KnowledgeError(f"table must be snake_case ASCII (got {table!r}; try {to_ident(table)!r}).")
            if not table_name or not _IDENT.match(table_name):
                raise KnowledgeError(f"Pass table=<snake_case English name> for {label!r}, e.g. table='receipts'.")
            if table_name in RESERVED_TABLES or table_name.startswith("sqlite_") or table_name.endswith("_fts"):
                raise KnowledgeError(f"Table name {table_name!r} is reserved; pass another table name.")
            clash = self._find_term(ds, label)
            if clash is None and any(t["name"] == table_name for t in self.store.tables(ds["id"])):
                raise KnowledgeError(f"A table named {table_name!r} already exists; pass another table name.")

        old_cols = {c["name"]: c for c in (spec or {}).get("columns", [])}
        new_specs: list[dict[str, Any]] = []
        problems: list[str] = []
        for raw in columns or []:
            try:
                new_specs.append(self._column_spec(ds, raw, table_name))
            except KnowledgeError as exc:
                problems.append(f"column {raw.get('name')!r}: {exc}")
        if problems:
            raise KnowledgeError("Nothing was changed:\n- " + "\n- ".join(problems))
        if not spec and not new_specs:
            raise KnowledgeError("A new entity needs at least one column.")

        merged: list[dict[str, Any]] = [dict(c) for c in (spec or {}).get("columns", [])]
        added: list[dict[str, Any]] = []
        for c in new_specs:
            if c["name"] in old_cols:
                prev = old_cols[c["name"]]
                if prev["type"] != c["type"] or prev.get("ref") != c.get("ref"):
                    problems.append(f"column {c['name']!r} is {prev['type']}; types cannot change (add a new column instead)")
                    continue
                for m in merged:
                    if m["name"] == c["name"]:
                        for k in ("display_name", "description", "required"):
                            if c.get(k) is not None:
                                m[k] = c[k]
            else:
                merged.append(c)
                added.append(c)
        if problems:
            raise KnowledgeError("Nothing was changed:\n- " + "\n- ".join(problems))

        names = {c["name"] for c in merged}
        new_keys = list(keys) if keys is not None else (spec or {}).get("keys", [])
        unknown = [k for k in new_keys if k not in names]
        if unknown:
            raise KnowledgeError(f"keys {unknown} are not columns of {label}.")
        rows = self._count(table_name) if spec else 0
        if spec and keys is not None and sorted(keys) != sorted(spec.get("keys", [])) and rows:
            raise KnowledgeError(f"{label} already has {rows} records; keys can only change while it is empty.")

        with self._write() as con:
            if not spec:
                cols_sql = ["id INTEGER PRIMARY KEY"] + [self._column_ddl(c) for c in merged] + [
                    f"_source_id INTEGER REFERENCES {SOURCES_TABLE}(id)",
                    "_source_locator TEXT", "_evidence TEXT", "_note TEXT",
                    "_created_at TEXT NOT NULL", "_updated_at TEXT NOT NULL",
                ]
                con.execute(f"CREATE TABLE {q(table_name)} ({', '.join(cols_sql)})")
            else:
                for c in added:
                    con.execute(f"ALTER TABLE {q(table_name)} ADD COLUMN {self._column_ddl(c)}")

        new_spec = {"table": table_name, "columns": merged, "keys": new_keys, "label": self._label_column(merged, new_keys)}
        self._refresh(ds)
        stamp = now()
        if existing is None:
            tid = new_id("term")
            table_row = next(t for t in self.store.tables(ds["id"]) if t["name"] == table_name)
            self.store.insert(
                "term",
                {
                    "id": tid, "datasource_id": ds["id"], "name": label, "description": description or "",
                    "synonyms": [s.strip() for s in synonyms or [] if s.strip()], "status": "draft",
                    "primary_table_id": table_row["id"], "kind": "entity", "spec": new_spec,
                    "created_at": stamp, "updated_at": stamp,
                },
            )
        else:
            tid = existing["id"]
            table_row = next(t for t in self.store.tables(ds["id"]) if t["name"] == table_name)
            values: dict[str, Any] = {"spec": new_spec, "kind": "entity", "primary_table_id": table_row["id"], "updated_at": stamp}
            if description is not None and description != (existing.get("description") or ""):
                values["description"] = description
                if existing["status"] == "certified":
                    values.update(status="draft", certified_at=None)
            if synonyms:
                merged_syn = list(existing.get("synonyms") or [])
                for s in synonyms:
                    if s.strip() and s.strip().casefold() not in {x.casefold() for x in merged_syn}:
                        merged_syn.append(s.strip())
                values["synonyms"] = merged_syn
            self.store.update("term", tid, values)
        self._sync_attributes(ds, tid, label, new_spec)
        self.store.log("knowledge_entity", tid, "define", {"table": table_name, "added": [c["name"] for c in added]})
        self.index.sync()
        return {
            "entity": label,
            "table": table_name,
            "created": existing is None or not spec,
            "added_columns": [c["name"] for c in added],
            "columns": [self._column_view(c) for c in merged],
            "keys": new_keys,
            "label_column": new_spec["label"],
            "records": self._count(table_name),
            "next_step": "Add rows with add_records(entity, records=[{column: value, ...}], source={...}).",
        }

    def _column_spec(self, ds: dict[str, Any], raw: dict[str, Any], table_name: str) -> dict[str, Any]:
        name = (raw.get("name") or "").strip()
        ctype, ref = _parse_type(raw.get("type"), raw.get("ref"))
        ident = to_ident(name)
        if not ident or ident != name:
            raise KnowledgeError(f"name must be snake_case ASCII (try {ident or 'amount_krw'!r})")
        if ident == "id" or ident.startswith("_"):
            raise KnowledgeError("'id' and names starting with '_' are reserved for the store")
        spec: dict[str, Any] = {
            "name": ident,
            "type": ctype,
            "display_name": clean_label(raw.get("display_name") or "") or None,
            "description": raw.get("description"),
            "required": bool(raw.get("required")),
        }
        if ctype == "ref":
            if not ref:
                raise KnowledgeError("ref columns need a target: type='ref:Merchant'")
            target = self._find_term(ds, ref)
            if target is None or not target.get("spec"):
                raise KnowledgeError(f"ref target {ref!r} is not an entity yet; define it first")
            spec["ref"] = target["name"]
            spec["ref_table"] = target["spec"]["table"]
            if not ident.endswith("_id"):
                spec["name"] = f"{ident}_id"
            spec["display_name"] = spec["display_name"] or target["name"]
        return spec

    @staticmethod
    def _column_ddl(c: dict[str, Any]) -> str:
        if c["type"] == "ref":
            return f"{q(c['name'])} INTEGER REFERENCES {q(c['ref_table'])}(id)"
        return f"{q(c['name'])} {TYPES[c['type']]}"

    @staticmethod
    def _label_column(columns: list[dict[str, Any]], keys: list[str]) -> str | None:
        by_name = {c["name"]: c for c in columns}
        for k in keys:
            if by_name.get(k, {}).get("type") == "text":
                return k
        for n in _LABEL_COLUMNS:
            if by_name.get(n, {}).get("type") == "text":
                return n
        for c in columns:
            if c["type"] == "text":
                return c["name"]
        return None

    @staticmethod
    def _column_view(c: dict[str, Any]) -> dict[str, Any]:
        out = {"name": c["name"], "type": c["type"] if c["type"] != "ref" else f"ref:{c['ref']}"}
        for k in ("display_name", "description"):
            if c.get(k):
                out[k] = c[k]
        if c.get("required"):
            out["required"] = True
        return out

    def _count(self, table: str) -> int:
        if not self.path.exists():
            return 0
        with self._write() as con:
            try:
                return con.execute(f"SELECT COUNT(*) FROM {q(table)}").fetchone()[0]
            except sqlite3.OperationalError:
                return 0

    def _delete_entity(self, ds: dict[str, Any], term: dict[str, Any] | None, label: str, force: bool) -> dict[str, Any]:
        if term is None or not term.get("spec"):
            raise KnowledgeError(f"No knowledge entity named {label!r}.")
        if is_builtin(term):
            raise KnowledgeError(_builtin_message(term))
        table = term["spec"]["table"]
        rows = self._count(table)
        if rows and not force:
            raise KnowledgeError(f"{term['name']} has {rows} records. Pass force=true to delete the entity and its records.")
        referencing = [
            t["name"] for t in self.store.terms(ds["id"])
            if t.get("spec") and any(c.get("ref_table") == table for c in t["spec"]["columns"])
        ]
        if referencing:
            raise KnowledgeError(f"{', '.join(referencing)} reference {term['name']}; remove those first.")
        with self._write() as con:
            con.execute(f"DROP TABLE {q(table)}")
        with self.store.tx() as con:
            con.execute("DELETE FROM term WHERE id = ?", (term["id"],))
        self._refresh(ds)
        self.store.log("knowledge_entity", term["id"], "delete", {"table": table, "records": rows})
        self.index.sync()
        return {"deleted": term["name"], "records_deleted": rows}

    # ------------------------------------------------------------- sources
    def _source_id(self, con: sqlite3.Connection, source: Any) -> int | None:
        if source is None:
            return None
        if isinstance(source, bool):
            raise KnowledgeError("source must be an id or an object with a title")
        if isinstance(source, (int, str)) and str(source).removeprefix("source:").isdigit():
            sid = int(str(source).removeprefix("source:"))
            if not con.execute(f"SELECT 1 FROM {SOURCES_TABLE} WHERE id = ?", (sid,)).fetchone():
                raise KnowledgeError(f"No source with id {sid}.")
            return sid
        if not isinstance(source, dict):
            raise KnowledgeError("source must be an id or an object with a title")
        if source.get("id") is not None:
            sid = self._source_id(con, source["id"])
            if source.get("original"):
                path = vault.link_existing(self.vault_root, str(source["original"]))
                con.execute(f"UPDATE {SOURCES_TABLE} SET original = ? WHERE id = ?", (path, sid))
            self._store_original(con, sid, source)
            return sid
        title = (source.get("title") or "").strip()
        if not title:
            raise KnowledgeError("source needs a title (e.g. the file name or page title)")
        values = {k: source.get(k) for k in SOURCE_FIELDS if source.get(k) not in (None, "")}
        if "published_on" in values:
            values["published_on"] = coerce(values["published_on"], "date")
        if "original" in values:
            values["original"] = vault.link_existing(self.vault_root, str(values["original"]))
        sid = self._upsert_source(con, title, values)
        self._store_original(con, sid, source)
        return sid

    def _upsert_source(self, con: sqlite3.Connection, title: str, values: dict[str, Any]) -> int:
        row = None
        if values.get("uri"):
            row = con.execute(f"SELECT id FROM {SOURCES_TABLE} WHERE uri = ?", (values["uri"],)).fetchone()
        if row is None:
            row = con.execute(
                f"SELECT id FROM {SOURCES_TABLE} WHERE lower(title) = lower(?) AND COALESCE(kind, '') = COALESCE(?, '')",
                (title, values.get("kind")),
            ).fetchone()
        if row:
            updates = {k: v for k, v in values.items() if k != "title"}
            if updates:
                sets = ", ".join(f"{k} = ?" for k in updates)
                con.execute(f"UPDATE {SOURCES_TABLE} SET {sets} WHERE id = ?", (*updates.values(), row["id"]))
            return int(row["id"])
        values["title"] = title
        values["captured_at"] = datetime.now(UTC).replace(microsecond=0).isoformat()
        cols = ", ".join(values)
        cur = con.execute(f"INSERT INTO {SOURCES_TABLE} ({cols}) VALUES ({', '.join('?' for _ in values)})", tuple(values.values()))
        return int(cur.lastrowid)

    def _store_original(self, con: sqlite3.Connection, sid: int, source: dict[str, Any]) -> None:
        """Keep the full original in the Obsidian vault: write transcribed text or copy a local file.

        A source that already has an original keeps it; pass original=<vault path> to point it elsewhere.
        """
        text, file = source.get("original_text"), source.get("original_file")
        if not text and not file:
            return
        row = dict(con.execute(f"SELECT * FROM {SOURCES_TABLE} WHERE id = ?", (sid,)).fetchone())
        if row.get("original") and (self.vault_root / row["original"]).exists():
            return
        if file:
            path = vault.copy_file(self.vault_root, self.originals_dir, str(file))
        else:
            path = vault.write_text(self.vault_root, self.originals_dir, row, str(text))
        con.execute(f"UPDATE {SOURCES_TABLE} SET original = ? WHERE id = ?", (path, sid))

    # -------------------------------------------------------------- records
    def _resolve_ref(
        self, con: sqlite3.Connection, ds: dict[str, Any], column: dict[str, Any], value: Any,
        create: bool, source_id: int | None, created: dict[str, list[str]],
    ) -> int | None:
        if value is None or value == "":
            return None
        target = self.entity(ds, column["ref"])
        tspec, table = target["spec"], target["spec"]["table"]
        if isinstance(value, bool):
            raise KnowledgeError("a reference cannot be a boolean")
        if isinstance(value, int) or (isinstance(value, str) and value.strip().isdigit() and not tspec.get("label")):
            row = con.execute(f"SELECT id FROM {q(table)} WHERE id = ?", (int(value),)).fetchone()
            if not row:
                raise KnowledgeError(f"{target['name']} has no record with id {value}")
            return int(row["id"])
        if isinstance(value, dict):
            fields = value
        else:
            label = tspec.get("label")
            if not label:
                raise KnowledgeError(f"{target['name']} has no text column to match {value!r}; pass its id")
            fields = {label: value}
        existing = self._match(con, target, self._coerce_fields(target, fields, strict=False))
        if existing is not None:
            return existing
        if not create:
            raise KnowledgeError(f"no {target['name']} matches {value!r} (set create_missing_refs to add it)")
        values = self._coerce_fields(target, fields, strict=True)
        missing = [c["name"] for c in tspec["columns"] if c.get("required") and values.get(c["name"]) is None]
        if missing:
            raise KnowledgeError(f"cannot create {target['name']} {value!r}: required {missing}")
        rid = self._insert(con, table, values, source_id, None, None, None)
        created.setdefault(target["name"], []).append(str(value if not isinstance(value, dict) else fields))
        return rid

    def _coerce_fields(self, term: dict[str, Any], fields: dict[str, Any], strict: bool) -> dict[str, Any]:
        cols = {c["name"]: c for c in term["spec"]["columns"]}
        aliases = {c["name"][:-3]: c["name"] for c in cols.values() if c["type"] == "ref"}
        out: dict[str, Any] = {}
        for key, value in fields.items():
            name = aliases.get(key, key)
            col = cols.get(name)
            if col is None:
                if strict:
                    raise KnowledgeError(f"{term['name']} has no column {key!r}")
                continue
            out[name] = value if col["type"] == "ref" else coerce(value, col["type"])
        return out

    def _match(self, con: sqlite3.Connection, term: dict[str, Any], values: dict[str, Any]) -> int | None:
        """Existing row with the same keys (or, without keys, the same label / all given values)."""
        spec = term["spec"]
        table = spec["table"]
        keys = spec.get("keys") or []
        if keys and all(values.get(k) is not None for k in keys):
            probe = {k: values[k] for k in keys}
        elif not keys and spec.get("label") and set(values) == {spec["label"]}:
            probe = {spec["label"]: values[spec["label"]]}
        elif not keys and values and all(not isinstance(v, dict) for v in values.values()):
            probe = {k: v for k, v in values.items() if not isinstance(v, (dict, list))}
        else:
            return None
        conds, params = [], []
        for k, v in probe.items():
            if v is None:
                conds.append(f"{q(k)} IS NULL")
            elif isinstance(v, str):
                conds.append(f"lower(trim({q(k)})) = lower(trim(?))")
                params.append(v)
            else:
                conds.append(f"{q(k)} = ?")
                params.append(v)
        rows = con.execute(f"SELECT id FROM {q(table)} WHERE {' AND '.join(conds)} LIMIT 2", params).fetchall()
        if len(rows) > 1 and keys:
            raise KnowledgeError(f"keys {keys} match several {term['name']} records")
        return int(rows[0]["id"]) if rows else None

    def _insert(self, con, table, values, source_id, locator, evidence, note) -> int:
        stamp = now()
        row = {**values, "_source_id": source_id, "_source_locator": locator, "_evidence": evidence, "_note": note,
               "_created_at": stamp, "_updated_at": stamp}
        cols = ", ".join(q(k) for k in row)
        cur = con.execute(f"INSERT INTO {q(table)} ({cols}) VALUES ({', '.join('?' for _ in row)})", tuple(row.values()))
        return int(cur.lastrowid)

    def _update(self, con, table, rid, values, source_id, locator, evidence, note) -> None:
        row = dict(values)
        for k, v in (("_source_id", source_id), ("_source_locator", locator), ("_evidence", evidence), ("_note", note)):
            if v is not None:
                row[k] = v
        row["_updated_at"] = now()
        sets = ", ".join(f"{q(k)} = ?" for k in row)
        con.execute(f"UPDATE {q(table)} SET {sets} WHERE id = ?", (*row.values(), rid))

    def add_records(
        self,
        entity: str,
        records: list[dict[str, Any]],
        source: Any = None,
        on_conflict: str = "update",
        create_missing_refs: bool = True,
    ) -> dict[str, Any]:
        ds = self.ds()
        term = self.entity(ds, entity)
        spec, table = term["spec"], term["spec"]["table"]
        if not records:
            raise KnowledgeError("records is empty.")
        if len(records) > MAX_RECORDS:
            raise KnowledgeError(f"At most {MAX_RECORDS} records per call.")
        if on_conflict not in ("update", "skip", "error"):
            raise KnowledgeError("on_conflict must be update, skip or error.")
        cols = {c["name"]: c for c in spec["columns"]}
        aliases = {c["name"][:-3]: c["name"] for c in cols.values() if c["type"] == "ref"}

        # Validate everything first so a bad row writes nothing.
        prepared, problems = [], []
        for i, rec in enumerate(records, 1):
            if not isinstance(rec, dict):
                problems.append(f"record {i}: expected an object")
                continue
            values, refs = {}, {}
            for key, value in rec.items():
                if key in RECORD_META_KEYS:
                    continue
                name = aliases.get(key, key)
                col = cols.get(name)
                if col is None:
                    problems.append(f"record {i}: unknown column {key!r} (columns: {', '.join(cols)}; add it with define_entity)")
                    continue
                if col["type"] == "ref":
                    refs[name] = value
                    continue
                try:
                    values[name] = coerce(value, col["type"])
                except KnowledgeError as exc:
                    problems.append(f"record {i}: {key}: {exc}")
            prepared.append((i, rec, values, refs))
        if problems:
            raise KnowledgeError("Nothing was saved. Fix these and call add_records again:\n- " + "\n- ".join(problems[:40]))

        result = {"inserted": [], "updated": [], "unchanged": [], "skipped": []}
        created_refs: dict[str, list[str]] = {}
        touched = {table}
        with self._write() as con:
            batch_source = self._source_id(con, source)
            if batch_source is not None:
                touched.add(SOURCES_TABLE)
            for i, rec, values, refs in prepared:
                try:
                    sid = self._source_id(con, rec["_source"]) if rec.get("_source") is not None else batch_source
                    for name, value in refs.items():
                        values[name] = self._resolve_ref(con, ds, cols[name], value, create_missing_refs, sid, created_refs)
                        touched.add(cols[name]["ref_table"])
                    if rec.get("id") is not None:
                        rid = int(rec["id"])
                        if not con.execute(f"SELECT 1 FROM {q(table)} WHERE id = ?", (rid,)).fetchone():
                            raise KnowledgeError(f"no {term['name']} with id {rid}")
                    else:
                        rid = self._match(con, term, values) if values else None
                    if rid is None:
                        missing = [c for c, s in cols.items() if s.get("required") and values.get(c) is None]
                        if missing:
                            raise KnowledgeError(f"required {missing} missing")
                        rid = self._insert(con, table, values, sid, rec.get("_locator"), rec.get("_evidence"), rec.get("_note"))
                        result["inserted"].append(rid)
                        continue
                    if rec.get("id") is None and on_conflict == "error":
                        raise KnowledgeError(f"matches existing {term['name']} id {rid}")
                    if rec.get("id") is None and on_conflict == "skip":
                        result["skipped"].append(rid)
                        continue
                    current = dict(con.execute(f"SELECT * FROM {q(table)} WHERE id = ?", (rid,)).fetchone())
                    changes = {k: v for k, v in values.items() if current.get(k) != v}
                    meta_changed = any(rec.get(k) is not None for k in ("_locator", "_evidence", "_note")) or (
                        sid is not None and current.get("_source_id") != sid
                    )
                    if not changes and not meta_changed:
                        result["unchanged"].append(rid)
                        continue
                    self._update(con, table, rid, changes, sid, rec.get("_locator"), rec.get("_evidence"), rec.get("_note"))
                    result["updated"].append(rid)
                except (KnowledgeError, sqlite3.Error) as exc:
                    raise KnowledgeError(f"Nothing was saved. record {i}: {exc}") from exc
        self._refresh(ds, tuple(touched))
        self.store.log("knowledge_records", term["id"], "add", {k: len(v) for k, v in result.items()})
        self.index.sync()
        return {
            "entity": term["name"],
            "table": table,
            **{k: v for k, v in result.items()},
            "created_references": created_refs,
            "source_id": batch_source,
            "total_records": self._count(table),
        }

    def edit_records(self, entity: str, action: str, ids: list[int], values: dict[str, Any] | None = None, source: Any = None) -> dict[str, Any]:
        ds = self.ds()
        term = self.entity(ds, entity)
        spec, table = term["spec"], term["spec"]["table"]
        if not ids:
            raise KnowledgeError("ids is empty; find them with run_sql or search first.")
        ids = [int(i) for i in ids][:MAX_RECORDS]
        marks = ",".join("?" for _ in ids)
        with self._write() as con:
            found = {r[0] for r in con.execute(f"SELECT id FROM {q(table)} WHERE id IN ({marks})", ids)}
            missing = [i for i in ids if i not in found]
            if missing:
                raise KnowledgeError(f"No {term['name']} records with ids {missing}.")
            if action == "delete":
                blockers = []
                for other in self.store.terms(ds["id"]):
                    ospec = other.get("spec")
                    for c in (ospec or {}).get("columns", []):
                        if c.get("ref_table") == table:
                            n = con.execute(f"SELECT COUNT(*) FROM {q(ospec['table'])} WHERE {q(c['name'])} IN ({marks})", ids).fetchone()[0]
                            if n:
                                blockers.append(f"{n} {other['name']} record(s) via {c['name']}")
                if blockers:
                    raise KnowledgeError("Not deleted; still referenced by " + ", ".join(blockers) + ". Update or delete those first.")
                con.execute(f"DELETE FROM {q(table)} WHERE id IN ({marks})", ids)
                touched = {table}
                result = {"deleted": ids}
            elif action == "update":
                if not values:
                    raise KnowledgeError("update needs values.")
                cols = {c["name"]: c for c in spec["columns"]}
                aliases = {c["name"][:-3]: c["name"] for c in cols.values() if c["type"] == "ref"}
                sid = self._source_id(con, source)
                created: dict[str, list[str]] = {}
                clean: dict[str, Any] = {}
                touched = {table}
                for key, value in values.items():
                    if key in ("_locator", "_evidence", "_note"):
                        continue
                    name = aliases.get(key, key)
                    col = cols.get(name)
                    if col is None:
                        raise KnowledgeError(f"{term['name']} has no column {key!r}.")
                    if col["type"] == "ref":
                        clean[name] = self._resolve_ref(con, ds, col, value, True, sid, created)
                        touched.add(col["ref_table"])
                    else:
                        clean[name] = coerce(value, col["type"])
                for rid in ids:
                    self._update(con, table, rid, clean, sid, values.get("_locator"), values.get("_evidence"), values.get("_note"))
                result = {"updated": ids, "created_references": created}
            else:
                raise KnowledgeError("action must be update or delete.")
        self._refresh(ds, tuple(touched))
        self.store.log("knowledge_records", term["id"], action, {"ids": ids})
        self.index.sync()
        return {"entity": term["name"], **result, "total_records": self._count(table)}

    # ------------------------------------------------------------- concepts
    def save_concept(
        self,
        name: str,
        description: str | None = None,
        body: str | None = None,
        append_body: str | None = None,
        synonyms: list[str] | None = None,
        relations: list[dict[str, Any]] | None = None,
        remove_relations: list[dict[str, Any]] | None = None,
        source: Any = None,
        locator: str | None = None,
        evidence: str | None = None,
        status: str | None = None,
        datasource: str | None = None,
    ) -> dict[str, Any]:
        ds = self.svc.ds(datasource) if datasource else self.ds()
        label = clean_label(name)
        if not label:
            raise KnowledgeError("Give the concept a name.")
        if status and status not in ("draft", "certified", "deprecated"):
            raise KnowledgeError("status must be draft, certified or deprecated.")
        rels = []
        for r in relations or []:
            rel = to_ident(r.get("relation") or "")
            if not _RELATION.match(rel):
                raise KnowledgeError(f"relation {r.get('relation')!r} must be a short snake_case verb such as {', '.join(SUGGESTED_RELATIONS[:6])}.")
            if not (r.get("target") or "").strip():
                raise KnowledgeError("each relation needs a target Term name.")
            rels.append({**r, "relation": rel})

        source_id = None
        if source is not None:
            self.ds()
            with self._write() as con:
                source_id = self._source_id(con, source)

        term = self._find_term(ds, label)
        stamp = now()
        created = term is None
        if created:
            tid = new_id("term")
            self.store.insert(
                "term",
                {
                    "id": tid, "datasource_id": ds["id"], "name": label, "description": description or "",
                    "synonyms": [s.strip() for s in synonyms or [] if s.strip()], "status": status or "draft",
                    "kind": "concept", "body": (body or append_body or None), "created_at": stamp, "updated_at": stamp,
                    "certified_at": stamp if status == "certified" else None,
                },
            )
        else:
            tid = term["id"]
            values: dict[str, Any] = {}
            meaning = False
            if description is not None and description != (term.get("description") or ""):
                values["description"] = description
                meaning = True
            new_body = term.get("body") or ""
            if body is not None:
                new_body = body
            if append_body:
                new_body = (new_body.rstrip() + "\n\n" + append_body.strip()).strip()
            if new_body != (term.get("body") or ""):
                values["body"] = new_body
                meaning = True
            if synonyms:
                merged = list(term.get("synonyms") or [])
                for s in synonyms:
                    if s.strip() and s.strip().casefold() not in {x.casefold() for x in merged}:
                        merged.append(s.strip())
                values["synonyms"] = merged
            if status:
                values["status"] = status
                values["certified_at"] = stamp if status == "certified" else None
            elif meaning and term["status"] == "certified":
                values.update(status="draft", certified_at=None)
            if values:
                values["updated_at"] = stamp
                self.store.update("term", tid, values)

        stubs: list[str] = []
        with self.store.tx() as con:
            if source_id is not None:
                con.execute(
                    "INSERT OR REPLACE INTO term_source (term_id, source_id, locator, evidence, created_at) VALUES (?, ?, ?, ?, ?)",
                    (tid, source_id, locator, evidence, stamp),
                )
        for r in rels:
            target = self._find_term(ds, r["target"])
            if is_builtin(target) and self.is_knowledge(ds):
                raise KnowledgeError(f"{target['name']!r} is a built-in table of the store, not a Term to relate to.")
            if target is None:
                target_id = new_id("term")
                self.store.insert(
                    "term",
                    {
                        "id": target_id, "datasource_id": ds["id"], "name": clean_label(r["target"]), "description": "",
                        "synonyms": [], "status": "draft", "kind": "concept", "created_at": stamp, "updated_at": stamp,
                    },
                )
                stubs.append(clean_label(r["target"]))
            else:
                target_id = target["id"]
            with self.store.tx() as con:
                con.execute(
                    """
                    INSERT INTO term_relation (id, datasource_id, source_term_id, relation, target_term_id, description, source_id, evidence, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (source_term_id, relation, target_term_id)
                    DO UPDATE SET description = COALESCE(excluded.description, description),
                                  source_id = COALESCE(excluded.source_id, source_id),
                                  evidence = COALESCE(excluded.evidence, evidence)
                    """,
                    (new_id("rel"), ds["id"], tid, r["relation"], target_id, r.get("description"), source_id, r.get("evidence"), stamp),
                )
        removed = 0
        for r in remove_relations or []:
            target = self._find_term(ds, r.get("target") or "")
            if target is None:
                continue
            with self.store.tx() as con:
                cur = con.execute(
                    "DELETE FROM term_relation WHERE source_term_id = ? AND relation = ? AND target_term_id = ?",
                    (tid, to_ident(r.get("relation") or ""), target["id"]),
                )
                removed += cur.rowcount
        self.store.log("concept", tid, "create" if created else "update", {"relations": len(rels), "removed": removed})
        if self.is_knowledge(ds):
            self.index.sync()
        view = self.svc.get_term(tid, datasource=ds["name"])
        return {"created": created, "stub_concepts_created": stubs, "relations_removed": removed, "concept": view}

    # ------------------------------------------------------------- passages
    def save_passages(
        self, passages: list[dict[str, Any]] | None = None, source: Any = None, delete_ids: list[int] | None = None
    ) -> dict[str, Any]:
        return self.index.save_passages(passages, source, delete_ids)

    def search_text(
        self, query: str, kinds: list[str] | None = None, limit: int = 10, source: int | None = None, term: str | None = None
    ) -> dict[str, Any]:
        return self.index.search(query, kinds, limit, source, term)

    # -------------------------------------------------------------- reading
    def relations_view(self, term_id: str) -> list[dict[str, Any]]:
        rows = self.store.all(
            """
            SELECT r.relation, r.description, r.source_id, r.evidence,
                   s.id AS from_id, s.name AS from_name, t.id AS to_id, t.name AS to_name
            FROM term_relation r JOIN term s ON s.id = r.source_term_id JOIN term t ON t.id = r.target_term_id
            WHERE r.source_term_id = ? OR r.target_term_id = ?
            ORDER BY r.relation, s.name, t.name
            """,
            (term_id, term_id),
        )
        out = []
        for r in rows:
            outgoing = r["from_id"] == term_id
            item = {
                "direction": "outgoing" if outgoing else "incoming",
                "relation": r["relation"],
                "term": r["to_name"] if outgoing else r["from_name"],
            }
            for k in ("description", "evidence", "source_id"):
                if r.get(k) is not None:
                    item[k] = r[k]
            out.append(item)
        return out

    def term_sources(self, term_id: str) -> list[dict[str, Any]]:
        links = self.store.all("SELECT * FROM term_source WHERE term_id = ? ORDER BY created_at", (term_id,))
        if not links or not self.path.exists():
            return []
        ids = [link["source_id"] for link in links]
        with self._write() as con:
            rows = {r["id"]: dict(r) for r in con.execute(
                f"SELECT id, title, kind, uri FROM {SOURCES_TABLE} WHERE id IN ({','.join('?' for _ in ids)})", ids
            )}
        return [
            {**rows.get(link["source_id"], {"id": link["source_id"]}), **({"locator": link["locator"]} if link.get("locator") else {}),
             **({"evidence": link["evidence"]} if link.get("evidence") else {})}
            for link in links
        ]

    def entity_stats(self, term: dict[str, Any]) -> dict[str, Any]:
        spec = term.get("spec") or {}
        if not spec or not self.path.exists():
            return {}
        table = spec["table"]
        with self._write() as con:
            try:
                total = con.execute(f"SELECT COUNT(*) FROM {q(table)}").fetchone()[0]
                if is_builtin(term):
                    return {"records": total, "built_in": True}
                sourced = con.execute(f"SELECT COUNT(*) FROM {q(table)} WHERE _source_id IS NOT NULL").fetchone()[0]
                n_sources = con.execute(f"SELECT COUNT(DISTINCT _source_id) FROM {q(table)}").fetchone()[0]
            except sqlite3.OperationalError:
                return {}
        return {
            "records": total,
            "records_with_source": sourced,
            "distinct_sources": n_sources,
            "keys": spec.get("keys") or [],
            "label_column": spec.get("label"),
            "columns": [self._column_view(c) for c in spec["columns"]],
            "provenance": f"join {table}._source_id = sources.id to cite where a record came from",
        }

    def get_source(self, source: Any) -> dict[str, Any]:
        ds = self.ds(create=False)
        if ds is None or not self.path.exists():
            raise KnowledgeError("The knowledge store is empty.")
        with self._write() as con:
            if isinstance(source, (int,)) or str(source).removeprefix("source:").isdigit():
                row = con.execute(f"SELECT * FROM {SOURCES_TABLE} WHERE id = ?", (int(str(source).removeprefix("source:")),)).fetchone()
            else:
                row = con.execute(
                    f"SELECT * FROM {SOURCES_TABLE} WHERE lower(title) LIKE lower(?) OR uri = ? ORDER BY id DESC LIMIT 1",
                    (f"%{source}%", str(source)),
                ).fetchone()
            if row is None:
                raise KnowledgeError(f"No source matches {source!r}.")
            src = dict(row)
            derived = []
            for t in self.store.terms(ds["id"]):
                spec = t.get("spec")
                if not spec or is_builtin(t):
                    continue
                ids = [r[0] for r in con.execute(f"SELECT id FROM {q(spec['table'])} WHERE _source_id = ? ORDER BY id LIMIT 50", (src["id"],))]
                if ids:
                    n = con.execute(f"SELECT COUNT(*) FROM {q(spec['table'])} WHERE _source_id = ?", (src["id"],)).fetchone()[0]
                    derived.append({"entity": t["name"], "records": n, "ids": ids})
        content = src.get("content") or ""
        if len(content) > 4000:
            src["content"] = content[:4000] + f"… ({len(content)} characters)"
        concepts = self.store.all(
            "SELECT t.name, ts.locator, ts.evidence FROM term_source ts JOIN term t ON t.id = ts.term_id WHERE ts.source_id = ?",
            (src["id"],),
        )
        relations = self.store.all(
            """
            SELECT s.name AS source_term, r.relation, t.name AS target_term FROM term_relation r
            JOIN term s ON s.id = r.source_term_id JOIN term t ON t.id = r.target_term_id WHERE r.source_id = ?
            """,
            (src["id"],),
        )
        passages = self.index.passages_for(source_id=src["id"])
        out = {"source": src, "records": derived, "concepts": concepts, "relations": relations, "passages": passages}
        if src.get("original"):
            out["original_link"] = vault.wikilink(src["original"], label=src["title"])
        return out

    def sources_for_search(self) -> list[dict[str, Any]]:
        if not self.path.exists() or self.ds(create=False) is None:
            return []
        with self._write() as con:
            try:
                return [dict(r) for r in con.execute(
                    f"SELECT id, title, kind, uri, summary, substr(content, 1, 5000) AS content FROM {SOURCES_TABLE} ORDER BY id DESC LIMIT 2000"
                )]
            except sqlite3.OperationalError:
                return []

    def stats(self) -> dict[str, Any]:
        ds = self.ds(create=False)
        if ds is None:
            return {}
        terms = self.store.terms(ds["id"])
        entities = [t for t in terms if t.get("spec") and not is_builtin(t)]
        concepts = [t for t in terms if (t.get("kind") or "entity") == "concept"]
        stubs = [t["name"] for t in concepts if not (t.get("description") or t.get("body"))]
        records = without_source = 0
        for t in entities:
            st = self.entity_stats(t)
            records += st.get("records", 0)
            without_source += st.get("records", 0) - st.get("records_with_source", 0)
        n_sources = self.entity_stats(next((t for t in terms if t.get("spec", {}) and t["spec"]["table"] == SOURCES_TABLE), {})).get("records", 0) if terms else 0
        relations = self.store.scalar("SELECT COUNT(*) FROM term_relation WHERE datasource_id = ?", (ds["id"],)) or 0
        passages = originals = 0
        if self.path.exists():
            with self._write() as con:
                try:
                    passages = con.execute("SELECT COUNT(*) FROM passages").fetchone()[0]
                    originals = con.execute(f"SELECT COUNT(*) FROM {SOURCES_TABLE} WHERE original IS NOT NULL").fetchone()[0]
                except sqlite3.OperationalError:
                    pass
        return {
            "entities": [t["name"] for t in entities],
            "records": records,
            "records_without_source": without_source,
            "concepts": len(concepts),
            "stub_concepts": stubs[:30],
            "relations": relations,
            "sources": n_sources,
            "sources_with_original": originals,
            "passages": passages,
            "vault": str(self.vault_root),
            "full_text_search": self.index.fts_available(),
        }


def _builtin_message(term: dict[str, Any]) -> str:
    table = (term.get("spec") or {}).get("table")
    if table == SOURCES_TABLE:
        return "Source is built in; describe sources through the source parameter of the write tools."
    if table == "passages":
        return f"{term['name']} is built in; store passages with save_passages."
    return f"{term['name']} is built in and read-only; it mirrors or indexes what save_concept, define_entity and save_passages store."
