"""Passages, full-text search and the SQL view of Terms in the knowledge store.

Three things live here, all inside ``knowledge.sqlite`` so ``run_sql`` can
combine them with records and sources in one query:

- passages: excerpts and summaries of parts of a source (the original stays
  in the Obsidian vault), each linked to the Terms it is about;
- a mirror of the knowledge store's Terms, typed relations and Term sources
  (``terms``, ``term_relations``, ``term_sources``). The ontology store stays
  the source of truth; when its changelog moves, the rows that differ are
  copied over, so SQL (including ``WITH RECURSIVE``) can walk the concept graph;
- FTS5 indexes over passages, Terms and sources, ranked with bm25 and kept up
  to date by triggers, one row at a time.

The tokenizer is ``unicode61`` with prefix queries rather than ``trigram``:
trigram cannot match words shorter than three characters, which rules out most
Korean nouns (기억, 학습), while a prefix query also matches a word followed by
a Korean particle (기억 → 기억을). Substrings inside compounds (망각곡선) are
found by a LIKE pass that runs next to the FTS query.
"""

from __future__ import annotations

import re
import sqlite3
from typing import TYPE_CHECKING, Any

from .errors import KnowledgeError
from .naming import clean_label
from .store import new_id, now
from .vault import wikilink

if TYPE_CHECKING:  # pragma: no cover
    from .knowledge import Knowledge

TOKENIZER = "unicode61 remove_diacritics 2"
PASSAGES = "passages"
PASSAGE_TERMS = "passage_terms"
TERMS = "terms"
TERM_RELATIONS = "term_relations"
TERM_SOURCES = "term_sources"
PASSAGES_FTS = "passages_fts"
TERMS_FTS = "terms_fts"
SOURCES_FTS = "sources_fts"
FTS_TABLES = (PASSAGES_FTS, TERMS_FTS, SOURCES_FTS)
INDEX_TABLES = (PASSAGES, PASSAGE_TERMS, TERMS, TERM_RELATIONS, TERM_SOURCES, *FTS_TABLES)
PASSAGE_KINDS = ("quote", "summary", "note")
MAX_PASSAGE_CHARS = 5000
MAX_PASSAGES = 200
SEARCH_KINDS = ("passage", "term", "source")

TABLES_DDL = (
    f"""
    CREATE TABLE IF NOT EXISTS {PASSAGES} (
        id INTEGER PRIMARY KEY,
        source_id INTEGER NOT NULL REFERENCES sources(id),
        kind TEXT NOT NULL DEFAULT 'quote',
        heading TEXT,
        locator TEXT,
        text TEXT NOT NULL,
        note TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {TERMS} (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        kind TEXT NOT NULL,
        status TEXT NOT NULL,
        description TEXT,
        synonyms TEXT,
        body TEXT,
        record_table TEXT,
        updated_at TEXT
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {PASSAGE_TERMS} (
        passage_id INTEGER NOT NULL REFERENCES {PASSAGES}(id) ON DELETE CASCADE,
        term_id TEXT NOT NULL REFERENCES {TERMS}(id),
        PRIMARY KEY (passage_id, term_id)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {TERM_RELATIONS} (
        id TEXT PRIMARY KEY,
        from_term_id TEXT NOT NULL REFERENCES {TERMS}(id),
        from_term TEXT NOT NULL,
        relation TEXT NOT NULL,
        to_term_id TEXT NOT NULL REFERENCES {TERMS}(id),
        to_term TEXT NOT NULL,
        description TEXT,
        source_id INTEGER REFERENCES sources(id),
        evidence TEXT,
        created_at TEXT
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {TERM_SOURCES} (
        term_id TEXT NOT NULL REFERENCES {TERMS}(id),
        term TEXT NOT NULL,
        source_id INTEGER NOT NULL REFERENCES sources(id),
        locator TEXT,
        evidence TEXT,
        PRIMARY KEY (term_id, source_id)
    )
    """,
)
FTS_DDL = (
    f"CREATE VIRTUAL TABLE IF NOT EXISTS {PASSAGES_FTS} USING fts5("
    f"passage_id UNINDEXED, source_id UNINDEXED, heading, text, tokenize = '{TOKENIZER}')",
    f"CREATE VIRTUAL TABLE IF NOT EXISTS {TERMS_FTS} USING fts5("
    f"term_id UNINDEXED, name, synonyms, description, body, tokenize = '{TOKENIZER}')",
    f"CREATE VIRTUAL TABLE IF NOT EXISTS {SOURCES_FTS} USING fts5("
    f"source_id UNINDEXED, title, summary, content, tokenize = '{TOKENIZER}')",
)
INDEX_DDL = (
    f"CREATE INDEX IF NOT EXISTS {PASSAGES}_source ON {PASSAGES} (source_id)",
    f"CREATE INDEX IF NOT EXISTS {PASSAGE_TERMS}_term ON {PASSAGE_TERMS} (term_id)",
)


# (FTS table, base table, base column -> FTS column pairs). The base key is copied into an
# UNINDEXED column so SQL can join on it.
FTS_SOURCES = (
    (PASSAGES_FTS, PASSAGES, (("id", "passage_id"), ("source_id", "source_id"), ("heading", "heading"), ("text", "text"))),
    (TERMS_FTS, TERMS, (("id", "term_id"), ("name", "name"), ("synonyms", "synonyms"), ("description", "description"), ("body", "body"))),
    (SOURCES_FTS, "sources", (("id", "source_id"), ("title", "title"), ("summary", "summary"), ("content", "content"))),
)
FTS_TRIGGER_NAMES = tuple(f"{fts}_{op}" for fts, _, _ in FTS_SOURCES for op in ("ai", "ad", "au"))


def _trigger_ddl() -> tuple[str, ...]:
    out: list[str] = []
    for fts, base, pairs in FTS_SOURCES:
        # Trigger bodies read new.<base column>; name the FTS columns explicitly.
        cols = ", ".join(f for _, f in pairs)
        new = ", ".join(f"new.{b}" for b, _ in pairs)
        insert = f"INSERT INTO {fts} (rowid, {cols}) VALUES (new.rowid, {new});"
        watched = ", ".join(b for b, _ in pairs if b != "id")
        out += [
            f"CREATE TRIGGER IF NOT EXISTS {fts}_ai AFTER INSERT ON {base} BEGIN {insert} END",
            f"CREATE TRIGGER IF NOT EXISTS {fts}_ad AFTER DELETE ON {base} BEGIN DELETE FROM {fts} WHERE rowid = old.rowid; END",
            f"CREATE TRIGGER IF NOT EXISTS {fts}_au AFTER UPDATE OF {watched} ON {base} BEGIN "
            f"DELETE FROM {fts} WHERE rowid = old.rowid; {insert} END",
        ]
    return tuple(out)


TRIGGERS_DDL = _trigger_ddl()

# FTS key columns point at their base tables; FTS5 tables cannot declare foreign keys.
FTS_KEYS = (
    (PASSAGES_FTS, "passage_id", PASSAGES, "id"),
    (PASSAGES_FTS, "source_id", "sources", "id"),
    (TERMS_FTS, "term_id", TERMS, "id"),
    (SOURCES_FTS, "source_id", "sources", "id"),
)
# Hidden FTS5 columns the validator must accept: the table-named column used by
# MATCH, bm25() and snippet(), plus rank and rowid.
FTS_HIDDEN_COLUMNS = ("rank", "rowid")

_MATCH_HELP = (
    "Filter with {t} MATCH '\"word\"*' (quote each word and add * so 'word' also matches words with a Korean "
    "particle attached; several words separated by spaces must all match, join them with OR for any). "
    "Rank with ORDER BY bm25({t}) and show where it matched with snippet({t}, -1, '[', ']', '…', 12). "
    "MATCH finds whole words and their prefixes only; for a substring inside a compound (망각곡선) use LIKE '%곡선%' "
    "on the base table, or call search_text, which does both."
)


def _col(name: str, ctype: str, display: str, description: str | None = None) -> dict[str, Any]:
    return {"name": name, "type": ctype, "display_name": display, "description": description, "required": False}


BUILTINS: dict[str, dict[str, Any]] = {
    PASSAGES: {
        "name": "Source Passage",
        "synonyms": ["발췌"],
        "description": "An excerpt (kind quote) or a section summary (kind summary) of one source, saved with save_passages "
        "so it can be searched and cited. The full original stays in the Obsidian vault (sources.original); heading and "
        "locator say where in it. Join passages.source_id = sources.id.",
        "columns": [
            _col("id", "integer", "Passage ID", "Row id of the passage."),
            _col("source_id", "integer", "Source ID", "The source the passage comes from (sources.id)."),
            _col("kind", "text", "Passage Kind", "quote (words as written), summary (a section in other words) or note (the user's comment)."),
            _col("heading", "text", "Heading", "Section heading in the original, used for [[note#heading]] links."),
            _col("locator", "text", "Locator", "Page, timestamp or line in the original."),
            _col("text", "text", "Passage Text"),
            _col("note", "text", "Passage Note"),
            _col("created_at", "datetime", "Created At"),
            _col("updated_at", "datetime", "Updated At"),
        ],
    },
    PASSAGE_TERMS: {
        "name": "Passage Term Link",
        "synonyms": [],
        "description": "Which Terms a passage is about: one row per passage and Term. Join passage_terms.passage_id = "
        "passages.id and passage_terms.term_id = terms.id.",
        "columns": [
            _col("passage_id", "integer", "Passage ID"),
            _col("term_id", "text", "Term ID", "Id of the Term in the terms table."),
        ],
    },
    TERMS: {
        "name": "Ontology Term",
        "synonyms": ["온톨로지 용어"],
        "description": "Every entity and concept in the knowledge store, mirrored from the ontology so SQL can read it: "
        "name, kind (entity or concept), status, definition, synonyms and markdown body. Read-only; change Terms with "
        "save_concept, define_entity or update_term.",
        "columns": [
            _col("id", "text", "Term ID", "Term id, the same as in get_term."),
            _col("name", "text", "Term Name"),
            _col("kind", "text", "Term Kind", "entity (has records) or concept (has a body)."),
            _col("status", "text", "Term Status", "draft, certified or deprecated."),
            _col("description", "text", "Definition"),
            _col("synonyms", "text", "Synonyms", "Comma-separated other names."),
            _col("body", "text", "Body", "Markdown page of the Term."),
            _col("record_table", "text", "Record Table", "For entities, the table that holds their records."),
            _col("updated_at", "datetime", "Updated At"),
        ],
    },
    TERM_RELATIONS: {
        "name": "Ontology Relation",
        "synonyms": ["온톨로지 관계"],
        "description": "Typed links between Terms (is_a, part_of, causes, related_to, ...): one row per link from "
        "from_term to to_term, with the source that supports it. Walk chains with WITH RECURSIVE, e.g. every concept "
        "that is_a X directly or indirectly.",
        "columns": [
            _col("id", "text", "Relation ID"),
            _col("from_term_id", "text", "From Term ID"),
            _col("from_term", "text", "From Term"),
            _col("relation", "text", "Relation", "snake_case verb such as is_a, part_of, causes."),
            _col("to_term_id", "text", "To Term ID"),
            _col("to_term", "text", "To Term"),
            _col("description", "text", "Relation Description"),
            _col("source_id", "integer", "Source ID", "Source that supports the link (sources.id)."),
            _col("evidence", "text", "Evidence", "Quote that supports the link."),
            _col("created_at", "datetime", "Created At"),
        ],
    },
    TERM_SOURCES: {
        "name": "Ontology Term Source",
        "synonyms": [],
        "description": "Which sources a Term's definition or body came from, with locator and quote: one row per Term and source.",
        "columns": [
            _col("term_id", "text", "Term ID"),
            _col("term", "text", "Term Name"),
            _col("source_id", "integer", "Source ID"),
            _col("locator", "text", "Locator"),
            _col("evidence", "text", "Evidence"),
        ],
    },
    PASSAGES_FTS: {
        "name": "Passage Search Index",
        "synonyms": ["전문 검색"],
        "description": "Full-text index of passages (heading and text). " + _MATCH_HELP.format(t=PASSAGES_FTS)
        + " Join passages_fts.passage_id = passages.id.",
        "columns": [
            _col("passage_id", "integer", "Passage ID"),
            _col("source_id", "integer", "Source ID"),
            _col("heading", "text", "Indexed Heading"),
            _col("text", "text", "Indexed Text"),
        ],
    },
    TERMS_FTS: {
        "name": "Term Search Index",
        "synonyms": [],
        "description": "Full-text index of Term names, synonyms, definitions and bodies. " + _MATCH_HELP.format(t=TERMS_FTS)
        + " Join terms_fts.term_id = terms.id.",
        "columns": [
            _col("term_id", "text", "Term ID"),
            _col("name", "text", "Indexed Name"),
            _col("synonyms", "text", "Indexed Synonyms"),
            _col("description", "text", "Indexed Definition"),
            _col("body", "text", "Indexed Body"),
        ],
    },
    SOURCES_FTS: {
        "name": "Source Search Index",
        "synonyms": [],
        "description": "Full-text index of source titles, summaries and content. " + _MATCH_HELP.format(t=SOURCES_FTS)
        + " Join sources_fts.source_id = sources.id.",
        "columns": [
            _col("source_id", "integer", "Source ID"),
            _col("title", "text", "Indexed Title"),
            _col("summary", "text", "Indexed Summary"),
            _col("content", "text", "Indexed Content"),
        ],
    },
}

# Korean particles stripped from query words so '망각곡선을' searches '망각곡선'.
_PARTICLES_1 = tuple("을를이가은는의에와과도만로")
_PARTICLES_2 = ("에서", "으로", "에게", "까지", "부터", "처럼", "보다", "이나", "이랑")
_HANGUL = re.compile(r"^[가-힣]+$")


def query_words(query: str) -> list[str]:
    words: list[str] = []
    for w in re.findall(r"\w+", query or ""):
        if _HANGUL.match(w):
            if len(w) >= 4 and w.endswith(_PARTICLES_2):
                w = w[:-2]
            elif len(w) >= 3 and w.endswith(_PARTICLES_1):
                w = w[:-1]
        if w.casefold() not in {x.casefold() for x in words}:
            words.append(w)
    return words[:12]


def fts_query(words: list[str], any_word: bool = False) -> str:
    return (" OR " if any_word else " ").join(f'"{w}"*' for w in words)


def _snippet(text: str, words: list[str], width: int = 70) -> str:
    low = (text or "").casefold()
    hits = [low.find(w.casefold()) for w in words if low.find(w.casefold()) >= 0]
    if not hits:
        return (text or "")[: width * 2]
    start = max(0, min(hits) - width)
    piece = text[start: start + width * 2]
    for w in sorted(words, key=len, reverse=True):
        piece = re.sub(re.escape(w), lambda m: f"[{m.group(0)}]", piece, flags=re.IGNORECASE)
    return ("…" if start else "") + piece + ("…" if start + width * 2 < len(text) else "")


def _apply_diff(
    con: sqlite3.Connection, table: str, keys: tuple[str, ...], columns: tuple[str, ...], wanted: dict[tuple, tuple]
) -> list[tuple]:
    """Make *table* hold exactly *wanted* ({key tuple: value tuple}), writing only rows that differ.

    Returns the keys it deleted.
    """
    have = {
        tuple(r[: len(keys)]): tuple(r[len(keys):])
        for r in con.execute(f"SELECT {', '.join(keys + columns)} FROM {table}")
    }
    gone = [k for k in have if k not in wanted]
    where = " AND ".join(f"{k} = ?" for k in keys)
    if gone:
        con.executemany(f"DELETE FROM {table} WHERE {where}", gone)
    changed = [k + v for k, v in wanted.items() if have.get(k) != v]
    if changed:
        sets = ", ".join(f"{c} = excluded.{c}" for c in columns)
        con.executemany(
            f"INSERT INTO {table} ({', '.join(keys + columns)}) VALUES ({', '.join('?' for _ in keys + columns)}) "
            f"ON CONFLICT ({', '.join(keys)}) DO UPDATE SET {sets}",
            changed,
        )
    return gone


class TextIndex:
    """Passages, the Term mirror and the FTS indexes of the knowledge store."""

    def __init__(self, knowledge: "Knowledge") -> None:
        self.k = knowledge
        self.store = knowledge.store
        self._ready: str | None = None
        self._fts: bool | None = None

    # ------------------------------------------------------------ schema
    def fts_available(self) -> bool:
        if self._fts is None:
            con = sqlite3.connect(":memory:")
            try:
                con.execute(f"CREATE VIRTUAL TABLE t USING fts5(x, tokenize = '{TOKENIZER}')")
                self._fts = True
            except sqlite3.Error:
                self._fts = False
            finally:
                con.close()
        return self._fts

    def ensure_schema(self, ds: dict[str, Any]) -> None:
        """Create the index tables and their built-in Terms; cheap once done in this process."""
        if self._ready == str(self.k.path) and self.k.path.exists():
            return
        from .knowledge import SOURCES_TABLE  # noqa: PLC0415 (circular import)

        with self.k._write() as con:
            before = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            source_cols = {r[1] for r in con.execute(f"PRAGMA table_info({SOURCES_TABLE})")}
            if "original" not in source_cols:
                con.execute(f"ALTER TABLE {SOURCES_TABLE} ADD COLUMN original TEXT")
            for ddl in TABLES_DDL + INDEX_DDL:
                con.execute(ddl)
            if self.fts_available():
                for ddl in FTS_DDL:
                    con.execute(ddl)
                triggers = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
                if not set(FTS_TRIGGER_NAMES) <= triggers:
                    # New store, or one from before the triggers: index everything once, then row by row.
                    for ddl in TRIGGERS_DDL:
                        con.execute(ddl)
                    self._rebuild_fts(con)
            after = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        names = {t["name"] for t in self.store.terms(ds["id"])}
        missing = [table for table, b in BUILTINS.items() if b["name"] not in names and table in after]
        if after - before or "original" not in source_cols or missing:
            self.k._refresh(ds)
            self.k._ensure_source_term(ds)
            for table, b in BUILTINS.items():
                if table in after:
                    spec = {"table": table, "columns": b["columns"], "keys": [], "label": None, "builtin": True}
                    term = self.k._ensure_term(ds, b["name"], b["description"], spec)
                    if b["synonyms"] and not term.get("synonyms"):
                        self.store.update("term", term["id"], {"synonyms": b["synonyms"]})
            self._ensure_fts_relationships(ds)
        self._ready = str(self.k.path)

    def _ensure_fts_relationships(self, ds: dict[str, Any]) -> None:
        cols = {(c["table_name"], c["name"]): c for c in self.store.columns_for_datasource(ds["id"])}
        for src_table, src_col, dst_table, dst_col in FTS_KEYS:
            s, d = cols.get((src_table, src_col)), cols.get((dst_table, dst_col))
            if not s or not d:
                continue
            if self.store.one("SELECT id FROM relationship WHERE source_column_id = ? AND target_column_id = ?", (s["id"], d["id"])):
                continue
            self.store.insert(
                "relationship",
                {
                    "id": new_id("rel"), "datasource_id": ds["id"], "source_column_id": s["id"], "target_column_id": d["id"],
                    "origin": "declared", "status": "accepted", "confidence": 1.0, "cardinality": "many_to_one",
                    "evidence": {"reasons": ["built-in full-text index key"]}, "is_default": 1, "updated_at": now(),
                },
            )

    def pseudo_columns(self, catalog_columns: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Hidden FTS5 columns as catalog rows, so the validator accepts MATCH, bm25(), snippet() and rank."""
        out = []
        seen = set()
        for c in catalog_columns:
            table = c["table_name"]
            if table not in FTS_TABLES or table in seen:
                continue
            seen.add(table)
            for name in (table, *FTS_HIDDEN_COLUMNS):
                out.append({**c, "id": f"fts:{table}:{name}", "name": name, "data_type": "TEXT", "is_unique": 0, "pseudo": True})
        return out

    # -------------------------------------------------------------- sync
    def sync(self, force: bool = False) -> None:
        """Bring the Term mirror up to date when the ontology changed since the last sync.

        Only rows that differ are written; the FTS triggers then reindex just those rows. Passages
        and sources live in this file, so their indexes follow every write through the triggers.
        """
        ds = self.k.ds(create=False)
        if ds is None or not self.k.path.exists():
            return
        self.ensure_schema(ds)
        seq = int(self.store.scalar("SELECT COALESCE(MAX(seq), 0) FROM changelog") or 0)
        with self.k._write(foreign_keys=False) as con:
            if not force and con.execute("PRAGMA user_version").fetchone()[0] == seq:
                return
            self._sync_mirror(con, ds)
            con.execute(f"PRAGMA user_version = {seq}")

    def _sync_mirror(self, con: sqlite3.Connection, ds: dict[str, Any]) -> None:
        from .knowledge import is_builtin  # noqa: PLC0415 (circular import)

        terms = {
            t["id"]: (t["name"], t.get("kind") or "entity", t["status"], t.get("description") or None,
                      ", ".join(t.get("synonyms") or []) or None, t.get("body") or None,
                      (t.get("spec") or {}).get("table"), t.get("updated_at"))
            for t in self.store.terms(ds["id"]) if not is_builtin(t)
        }
        gone = _apply_diff(
            con, TERMS, ("id",), ("name", "kind", "status", "description", "synonyms", "body", "record_table", "updated_at"),
            {(i,): row for i, row in terms.items()},
        )
        if gone:
            con.executemany(f"DELETE FROM {PASSAGE_TERMS} WHERE term_id = ?", gone)
        rels = self.store.all(
            """
            SELECT r.*, s.name AS from_name, t.name AS to_name FROM term_relation r
            JOIN term s ON s.id = r.source_term_id JOIN term t ON t.id = r.target_term_id
            WHERE r.datasource_id = ?
            """,
            (ds["id"],),
        )
        _apply_diff(
            con, TERM_RELATIONS, ("id",),
            ("from_term_id", "from_term", "relation", "to_term_id", "to_term", "description", "source_id", "evidence", "created_at"),
            {
                (r["id"],): (r["source_term_id"], r["from_name"], r["relation"], r["target_term_id"], r["to_name"],
                             r.get("description"), r.get("source_id"), r.get("evidence"), r.get("created_at"))
                for r in rels if r["source_term_id"] in terms and r["target_term_id"] in terms
            },
        )
        links = self.store.all(
            "SELECT ts.*, t.name AS term_name FROM term_source ts JOIN term t ON t.id = ts.term_id WHERE t.datasource_id = ?",
            (ds["id"],),
        )
        _apply_diff(
            con, TERM_SOURCES, ("term_id", "source_id"), ("term", "locator", "evidence"),
            {(lk["term_id"], lk["source_id"]): (lk["term_name"], lk.get("locator"), lk.get("evidence")) for lk in links if lk["term_id"] in terms},
        )

    def _rebuild_fts(self, con: sqlite3.Connection) -> None:
        """Reindex every row (a new store, or one written before the FTS triggers existed)."""
        for fts, base, pairs in FTS_SOURCES:
            con.execute(f"DELETE FROM {fts}")
            con.execute(
                f"INSERT INTO {fts} (rowid, {', '.join(f for _, f in pairs)}) SELECT rowid, {', '.join(b for b, _ in pairs)} FROM {base}"
            )

    # ---------------------------------------------------------- passages
    def save_passages(
        self, passages: list[dict[str, Any]] | None, source: Any = None, delete_ids: list[int] | None = None
    ) -> dict[str, Any]:
        from .knowledge import is_builtin  # noqa: PLC0415

        k = self.k
        ds = k.ds()
        self.ensure_schema(ds)
        passages = passages or []
        delete_ids = [int(i) for i in delete_ids or []]
        if not passages and not delete_ids:
            raise KnowledgeError("Pass passages to save or delete_ids to remove.")
        if len(passages) > MAX_PASSAGES:
            raise KnowledgeError(f"At most {MAX_PASSAGES} passages per call.")

        problems: list[str] = []
        prepared: list[dict[str, Any]] = []
        wanted_terms: dict[str, str] = {}
        for i, p in enumerate(passages, 1):
            if not isinstance(p, dict):
                problems.append(f"passage {i}: expected an object")
                continue
            text = (p.get("text") or "").strip()
            kind = (p.get("kind") or "quote").strip().lower()
            if not text:
                problems.append(f"passage {i}: text is empty")
            elif len(text) > MAX_PASSAGE_CHARS:
                problems.append(
                    f"passage {i}: {len(text)} characters; keep one idea per passage (at most {MAX_PASSAGE_CHARS}) and leave the full text in the original"
                )
            if kind not in PASSAGE_KINDS:
                problems.append(f"passage {i}: kind must be one of {', '.join(PASSAGE_KINDS)}")
            names = [clean_label(n) for n in (p.get("terms") or []) if isinstance(n, str) and clean_label(n)]
            for n in names:
                wanted_terms.setdefault(n.casefold(), n)
            prepared.append({**p, "text": text, "kind": kind, "terms": names})
        if passages and source is None and any(p.get("id") is None for p in prepared):
            problems.append("new passages need a source (an id or {title, kind, uri, original...})")
        if problems:
            raise KnowledgeError("Nothing was saved. Fix these and call save_passages again:\n- " + "\n- ".join(problems[:40]))

        referenced = sorted({*delete_ids, *(int(p["id"]) for p in prepared if p.get("id") is not None)})
        if referenced:
            with k._write() as con:
                marks = ",".join("?" for _ in referenced)
                known = {r[0] for r in con.execute(f"SELECT id FROM {PASSAGES} WHERE id IN ({marks})", referenced)}
            missing_ids = [i for i in referenced if i not in known]
            if missing_ids:
                raise KnowledgeError(f"Nothing was saved. No passages with ids {missing_ids}.")

        # Resolve Terms; missing ones become empty concepts, as relation targets do.
        term_ids: dict[str, str] = {}
        stubs: list[str] = []
        for key, name in wanted_terms.items():
            t = k._find_term(ds, name)
            if t is not None and is_builtin(t):
                raise KnowledgeError(f"Nothing was saved. {t['name']!r} is a built-in table, not a Term to link passages to.")
            if t is None:
                stamp = now()
                tid = new_id("term")
                self.store.insert(
                    "term",
                    {"id": tid, "datasource_id": ds["id"], "name": name, "description": "", "synonyms": [], "status": "draft",
                     "kind": "concept", "created_at": stamp, "updated_at": stamp},
                )
                self.store.log("concept", tid, "create", {"from": "save_passages"})
                stubs.append(name)
                term_ids[key] = tid
            else:
                term_ids[key] = t["id"]
        self.sync()  # passage_terms references the mirror, which must hold every Term first

        result: dict[str, list[int]] = {"inserted": [], "updated": [], "unchanged": [], "deleted": []}
        source_id = None
        with k._write() as con:
            if source is not None:
                source_id = k._source_id(con, source)
            if delete_ids:
                marks = ",".join("?" for _ in delete_ids)
                con.execute(f"DELETE FROM {PASSAGE_TERMS} WHERE passage_id IN ({marks})", delete_ids)
                con.execute(f"DELETE FROM {PASSAGES} WHERE id IN ({marks})", delete_ids)
                result["deleted"] = delete_ids
            for p in prepared:
                stamp = now()
                values = {"kind": p["kind"], "heading": p.get("heading") or None, "locator": p.get("locator") or None,
                          "text": p["text"], "note": p.get("note") or None}
                pid = int(p["id"]) if p.get("id") is not None else None
                if pid is None:
                    row = con.execute(
                        f"SELECT id FROM {PASSAGES} WHERE source_id = ? AND trim(text) = ?", (source_id, p["text"])
                    ).fetchone()
                    pid = int(row["id"]) if row else None
                if pid is None:
                    cur = con.execute(
                        f"INSERT INTO {PASSAGES} (source_id, kind, heading, locator, text, note, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (source_id, values["kind"], values["heading"], values["locator"], values["text"], values["note"], stamp, stamp),
                    )
                    pid = int(cur.lastrowid)
                    result["inserted"].append(pid)
                else:
                    current = dict(con.execute(f"SELECT * FROM {PASSAGES} WHERE id = ?", (pid,)).fetchone())
                    if p.get("id") is None:
                        values = {k_: v for k_, v in values.items() if v is not None}
                    if source_id is not None:
                        values["source_id"] = source_id
                    changes = {k_: v for k_, v in values.items() if current.get(k_) != v}
                    linked = {r[0] for r in con.execute(f"SELECT term_id FROM {PASSAGE_TERMS} WHERE passage_id = ?", (pid,))}
                    new_links = {term_ids[n.casefold()] for n in p["terms"]} - linked
                    if not changes and not new_links:
                        result["unchanged"].append(pid)
                        continue
                    if changes:
                        sets = ", ".join(f"{c} = ?" for c in changes)
                        con.execute(f"UPDATE {PASSAGES} SET {sets}, updated_at = ? WHERE id = ?", (*changes.values(), stamp, pid))
                    result["updated"].append(pid)
                for n in p["terms"]:
                    con.execute(f"INSERT OR IGNORE INTO {PASSAGE_TERMS} (passage_id, term_id) VALUES (?, ?)", (pid, term_ids[n.casefold()]))
            original = None
            if source_id is not None:
                row = con.execute("SELECT original FROM sources WHERE id = ?", (source_id,)).fetchone()
                original = row["original"] if row else None
            total = con.execute(f"SELECT COUNT(*) FROM {PASSAGES}").fetchone()[0]
        k._refresh(ds, (PASSAGES, PASSAGE_TERMS, "sources"))
        self.store.log("passages", str(source_id) if source_id else None, "save", {key: len(v) for key, v in result.items()})
        self.sync()
        return {
            **result,
            "source_id": source_id,
            "original": original,
            "stub_concepts_created": stubs,
            "total_passages": total,
            "next_step": "Find them with search_text, or combine with SQL: passages_fts MATCH '\"word\"*' joined to passages and sources.",
        }

    def passages_for(self, *, source_id: int | None = None, term_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        if not self.k.path.exists():
            return []
        where, params = [], []
        if source_id is not None:
            where.append("p.source_id = ?")
            params.append(source_id)
        if term_id is not None:
            where.append(f"p.id IN (SELECT passage_id FROM {PASSAGE_TERMS} WHERE term_id = ?)")
            params.append(term_id)
        sql = f"""
            SELECT p.*, s.title AS source_title, s.original AS original,
                   (SELECT group_concat(t.name, ', ') FROM {PASSAGE_TERMS} pt JOIN {TERMS} t ON t.id = pt.term_id WHERE pt.passage_id = p.id) AS term_names
            FROM {PASSAGES} p JOIN sources s ON s.id = p.source_id
            {('WHERE ' + ' AND '.join(where)) if where else ''}
            ORDER BY p.source_id, p.id LIMIT ?
        """
        with self.k._write() as con:
            try:
                rows = [dict(r) for r in con.execute(sql, (*params, limit))]
            except sqlite3.OperationalError:
                return []
        for r in rows:
            r["terms"] = [n for n in (r.pop("term_names") or "").split(", ") if n]
            if r.get("original"):
                r["link"] = wikilink(r["original"], r.get("heading"))
        return rows

    # ------------------------------------------------------------ search
    def search(
        self, query: str, kinds: list[str] | None = None, limit: int = 10, source: int | None = None, term: str | None = None
    ) -> dict[str, Any]:
        ds = self.k.ds(create=False)
        words = query_words(query)
        if not words:
            raise KnowledgeError("Give words to search for.")
        if ds is None or not self.k.path.exists():
            return {"query": query, "words": words, "hits": [], "hint": "The knowledge store is empty."}
        self.sync()
        wanted = [x for x in (kinds or SEARCH_KINDS) if x in SEARCH_KINDS] or list(SEARCH_KINDS)
        limit = max(1, min(int(limit or 10), 50))
        term_id = None
        if term:
            t = self.k._find_term(ds, term)
            if t is None:
                raise KnowledgeError(f"No Term named {term!r}.")
            term_id = t["id"]
        mode = "all words"
        with self.k._write() as con:
            hits = self._search(con, words, wanted, limit, source, term_id, any_word=False)
            if not hits and len(words) > 1:
                hits = self._search(con, words, wanted, limit, source, term_id, any_word=True)
                mode = "any word"
        hits.sort(key=lambda h: -h["score"])
        fq = fts_query(words, any_word=mode == "any word")
        out: dict[str, Any] = {
            "query": query,
            "words": words,
            "matched": mode,
            "hits": hits[:limit],
            "fts_query": fq,
            "sql_example": (
                "SELECT p.id, s.title, p.heading, p.locator, snippet(passages_fts, -1, '[', ']', '…', 12) AS hit "
                "FROM passages_fts JOIN passages p ON p.id = passages_fts.passage_id JOIN sources s ON s.id = p.source_id "
                f"WHERE passages_fts MATCH '{fq.replace(chr(39), chr(39) * 2)}' ORDER BY bm25(passages_fts) LIMIT 20"
            ),
        }
        if not hits:
            out["hint"] = "Nothing matched. Try the words as written in the source, a synonym or a translation."
        return out

    def _search(self, con, words, kinds, limit, source, term_id, any_word: bool) -> list[dict[str, Any]]:
        fts = self.fts_available()
        q = fts_query(words, any_word)
        hits: list[dict[str, Any]] = []

        def like(columns: list[str]) -> tuple[str, list[str]]:
            joiner = " OR " if any_word else " AND "
            parts, params = [], []
            for w in words:
                parts.append("(" + " OR ".join(f"{c} LIKE ?" for c in columns) + ")")
                params += [f"%{w}%"] * len(columns)
            return "(" + joiner.join(parts) + ")", params

        if "passage" in kinds:
            filters, fparams = [], []
            if source is not None:
                filters.append("p.source_id = ?")
                fparams.append(int(source))
            if term_id is not None:
                filters.append(f"p.id IN (SELECT passage_id FROM {PASSAGE_TERMS} WHERE term_id = ?)")
                fparams.append(term_id)
            extra = "".join(f" AND {f}" for f in filters)
            seen: set[int] = set()
            base = f"""
                SELECT p.id, p.source_id, p.kind, p.heading, p.locator, p.text, s.title, s.original,
                       (SELECT group_concat(t.name, ', ') FROM {PASSAGE_TERMS} pt JOIN {TERMS} t ON t.id = pt.term_id WHERE pt.passage_id = p.id) AS term_names
            """
            if fts:
                rows = con.execute(
                    base + f""", snippet({PASSAGES_FTS}, 3, '[', ']', '…', 16) AS snip, bm25({PASSAGES_FTS}, 0.0, 0.0, 2.0, 1.0) AS rank
                    FROM {PASSAGES_FTS} JOIN {PASSAGES} p ON p.id = {PASSAGES_FTS}.passage_id JOIN sources s ON s.id = p.source_id
                    WHERE {PASSAGES_FTS} MATCH ?{extra} ORDER BY rank LIMIT ?""",
                    (q, *fparams, limit),
                ).fetchall()
                for r in rows:
                    seen.add(r["id"])
                    hits.append(self._passage_hit(r, r["snip"], -r["rank"], "words"))
            if len(seen) < limit:  # substrings inside compounds; skipped when whole words already fill the page
                cond, lparams = like(["p.text", "COALESCE(p.heading, '')"])
                rows = con.execute(
                    base + f""" FROM {PASSAGES} p JOIN sources s ON s.id = p.source_id WHERE {cond}{extra} ORDER BY p.id DESC LIMIT ?""",
                    (*lparams, *fparams, limit * 2),
                ).fetchall()
                for r in rows:
                    if r["id"] not in seen:
                        hits.append(self._passage_hit(r, _snippet(r["text"], words), 0.0, "substring"))

        if "term" in kinds and source is None and term_id is None:
            seen_t: set[str] = set()
            filt, fparams = "", []
            if fts:
                rows = con.execute(
                    f"""SELECT t.id, t.name, t.kind, t.status, t.description,
                               snippet({TERMS_FTS}, -1, '[', ']', '…', 16) AS snip, bm25({TERMS_FTS}, 0.0, 10.0, 8.0, 3.0, 1.0) AS rank
                        FROM {TERMS_FTS} JOIN {TERMS} t ON t.id = {TERMS_FTS}.term_id
                        WHERE {TERMS_FTS} MATCH ?{filt} ORDER BY rank LIMIT ?""",
                    (q, *fparams, limit),
                ).fetchall()
                for r in rows:
                    seen_t.add(r["id"])
                    hits.append(self._term_hit(r, r["snip"], -r["rank"], "words"))
            if len(seen_t) < limit:
                cond, lparams = like(["t.name", "COALESCE(t.synonyms, '')", "COALESCE(t.description, '')", "COALESCE(t.body, '')"])
                rows = con.execute(
                    f"SELECT t.id, t.name, t.kind, t.status, t.description, t.body FROM {TERMS} t WHERE {cond}{filt} LIMIT ?",
                    (*lparams, *fparams, limit * 2),
                ).fetchall()
                for r in rows:
                    if r["id"] not in seen_t:
                        text = " ".join(x for x in (r["description"], r["body"]) if x) or r["name"]
                        hits.append(self._term_hit(r, _snippet(text, words), 0.0, "substring"))

        if "source" in kinds and term_id is None:
            seen_s: set[int] = set()
            filt = " AND s.id = ?" if source is not None else ""
            fparams = [int(source)] if source is not None else []
            if fts:
                rows = con.execute(
                    f"""SELECT s.id, s.title, s.kind, s.original,
                               snippet({SOURCES_FTS}, -1, '[', ']', '…', 16) AS snip, bm25({SOURCES_FTS}, 0.0, 8.0, 3.0, 1.0) AS rank
                        FROM {SOURCES_FTS} JOIN sources s ON s.id = {SOURCES_FTS}.source_id
                        WHERE {SOURCES_FTS} MATCH ?{filt} ORDER BY rank LIMIT ?""",
                    (q, *fparams, limit),
                ).fetchall()
                for r in rows:
                    seen_s.add(r["id"])
                    hits.append(self._source_hit(r, r["snip"], -r["rank"], "words"))
            if len(seen_s) < limit:
                cond, lparams = like(["s.title", "COALESCE(s.summary, '')", "COALESCE(s.content, '')"])
                rows = con.execute(
                    f"SELECT s.id, s.title, s.kind, s.original, s.summary, s.content FROM sources s WHERE {cond}{filt} LIMIT ?",
                    (*lparams, *fparams, limit * 2),
                ).fetchall()
                for r in rows:
                    if r["id"] not in seen_s:
                        text = " ".join(x for x in (r["summary"], r["content"]) if x) or r["title"]
                        hits.append(self._source_hit(r, _snippet(text, words), 0.0, "substring"))
        return hits

    @staticmethod
    def _passage_hit(r, snip: str, score: float, match: str) -> dict[str, Any]:
        hit: dict[str, Any] = {
            "kind": "passage", "passage_id": r["id"], "source_id": r["source_id"], "source": r["title"],
            "passage_kind": r["kind"], "snippet": snip, "score": round(score, 4), "match": match,
            "text": r["text"] if len(r["text"]) <= 600 else r["text"][:600] + "…",
        }
        for key in ("heading", "locator"):
            if r[key]:
                hit[key] = r[key]
        if r["term_names"]:
            hit["terms"] = r["term_names"].split(", ")
        if r["original"]:
            hit["original"] = r["original"]
            hit["link"] = wikilink(r["original"], r["heading"])
        return hit

    @staticmethod
    def _term_hit(r, snip: str, score: float, match: str) -> dict[str, Any]:
        return {"kind": "term", "term_id": r["id"], "name": r["name"], "term_kind": r["kind"], "status": r["status"],
                "snippet": snip, "score": round(score, 4), "match": match}

    @staticmethod
    def _source_hit(r, snip: str, score: float, match: str) -> dict[str, Any]:
        hit = {"kind": "source", "source_id": r["id"], "title": r["title"], "source_kind": r["kind"],
               "snippet": snip, "score": round(score, 4), "match": match}
        if r["original"]:
            hit["original"] = r["original"]
            hit["link"] = wikilink(r["original"])
        return hit
