"""Ontology operations behind the MCP tools.

Kept separate from the MCP layer so they can be tested and used from the CLI.
"""

from __future__ import annotations

import difflib
import json
from collections import defaultdict
from typing import Any

import yaml
from sqlglot import exp

from . import __version__, guidance
from .catalog import Scanner
from .config import Settings
from .datasources import Connections, mask_url, normalize_url, short_error, sqlglot_dialect
from .errors import KnowledgeError, OntologyError  # noqa: F401 (re-exported)
from .graph import JoinGraph, shared_parents
from .inference import names_own_table
from .knowledge import KNOWLEDGE_DS, SOURCES_TABLE, Knowledge, is_builtin
from .naming import clean_label, compact, id_base, to_label
from .profiling import is_numeric_type
from .search import build_docs, search
from .sqlcheck import SqlGuardError, analyze, extract_expressions, guard_readonly, guard_sources, normalize_sql
from .store import Store, new_id, now
from .validation import Validator, qtable

TERM_STATUSES = ("draft", "certified", "deprecated")
SQL_ATTR_KINDS = ("measure", "filter", "dimension", "join", "query")
_MEASURE_WORDS = {"amount", "price", "cost", "total", "revenue", "sales", "qty", "quantity", "profit", "margin", "value", "spend", "fee", "tax", "balance", "salary", "income", "discount"}


def _label(schema: str | None, name: str) -> str:
    return f"{schema}.{name}" if schema else name


def _shared_parent_note(shared: dict[str, Any]) -> str:
    parent, children = shared["parent"], shared["children"]
    listed = " and ".join(children) if len(children) == 2 else ", ".join(children)
    return (
        f"{listed} meet only at {parent}: each of them is joined one-to-many from {parent}, so the path pairs "
        f"every row of one with every row of the other for the same {parent} row. Use it for pairs or for a grouping "
        f"by one side; to count or sum them side by side, aggregate each in its own subquery or CTE and join the "
        f"results on {parent}. It does not say which row of one became or belongs to which row of the other."
    )


class OntologyService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.store = Store(settings.store_path)
        self.connections = Connections(settings.query_timeout_s)
        self.scanner = Scanner(self.store, self.connections, settings.profile_sample_rows)
        self.knowledge = Knowledge(self)

    # =============================================================== resolution
    def ds(self, name: str | None = None) -> dict[str, Any]:
        all_ds = self.store.datasources()
        if name:
            hit = self.store.datasource_by_name(name)
            if hit:
                return hit
            raise OntologyError(
                f"No datasource named {name!r}. Available: {', '.join(d['name'] for d in all_ds) or 'none'}."
            )
        if len(all_ds) == 1:
            return all_ds[0]
        if not all_ds:
            raise OntologyError(
                "No datasource is registered yet. Call add_datasource(name, url), or run "
                "`auto-ontology-mcp demo` in a terminal to load the sample retail database."
            )
        raise OntologyError(
            f"Several datasources exist ({', '.join(d['name'] for d in all_ds)}); pass datasource=<name>."
        )

    def dialect(self, ds: dict[str, Any]) -> str | None:
        return sqlglot_dialect(ds.get("dialect"))

    def table_ref(self, ds: dict[str, Any], ref: str) -> dict[str, Any]:
        ref = (ref or "").strip().strip('"`[]')
        tables = self.store.tables(ds["id"])
        if "." in ref:
            schema, name = ref.rsplit(".", 1)
            hits = [t for t in tables if t["name"].casefold() == name.casefold() and (t["schema_name"] or "").casefold() == schema.casefold()]
        else:
            hits = [t for t in tables if t["name"].casefold() == ref.casefold()]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise OntologyError(f"Table {ref!r} is ambiguous: {', '.join(_label(t['schema_name'], t['name']) for t in hits)}.")
        names = [_label(t["schema_name"], t["name"]) for t in tables]
        close = difflib.get_close_matches(ref, names, n=5, cutoff=0.5)
        raise OntologyError(f"Unknown table {ref!r}." + (f" Did you mean: {', '.join(close)}?" if close else ""))

    def column_ref(self, ds: dict[str, Any], ref: str) -> dict[str, Any]:
        if "." not in (ref or ""):
            raise OntologyError(f"Write columns as table.column (got {ref!r}).")
        table_part, col_name = ref.rsplit(".", 1)
        table = self.table_ref(ds, table_part)
        cols = self.store.columns(table["id"])
        for c in cols:
            if c["name"].casefold() == col_name.strip().strip('"`').casefold():
                return {**c, "table_name": table["name"], "schema_name": table["schema_name"], "table": table}
        close = difflib.get_close_matches(col_name, [c["name"] for c in cols], n=5, cutoff=0.5)
        raise OntologyError(f"Table {table['name']} has no column {col_name!r}." + (f" Did you mean: {', '.join(close)}?" if close else ""))

    def term_ref(self, ds: dict[str, Any], ref: str) -> dict[str, Any]:
        ref = (ref or "").strip()
        terms = self.store.terms(ds["id"])
        for t in terms:
            if t["id"] == ref or t["name"].casefold() == ref.casefold():
                return t
        for t in terms:
            if any(s.casefold() == ref.casefold() for s in t.get("synonyms") or []):
                return t
        hits = self._search(ds, ref, kinds=["term"], limit=5)
        suggestion = f" Closest Terms: {', '.join(h['name'] for h in hits)}." if hits else ""
        raise OntologyError(f"No Term named {ref!r}.{suggestion}")

    def _qualified(self, ds: dict[str, Any], table: dict[str, Any]) -> str:
        return qtable(table.get("schema_name") or None, table["name"]).sql(dialect=self.dialect(ds))

    def _catalog_tables(self, ds: dict[str, Any]) -> list[tuple[str, str]]:
        return [(t["schema_name"] or "", t["name"]) for t in self.store.tables(ds["id"])]

    def _run(self, ds: dict[str, Any], sql: str, max_rows: int | None = 1000) -> list[list[Any]]:
        return self.connections.run_readonly(ds, sql, max_rows=max_rows).rows

    # ================================================================ readiness
    def check_readiness(self) -> dict[str, Any]:
        out = []
        for ds in self.store.datasources():
            ok, err = self.connections.test(ds)
            tables = self.store.tables(ds["id"])
            terms = self.store.terms(ds["id"])
            gaps = self._modeling_gaps(ds)
            modeled, remaining = gaps["modeled"], gaps["without_terms"]
            changed = [t for t in tables if t["id"] in gaps["new_columns"]]
            rels = self.store.relationships(ds["id"], ("accepted", "proposed"))
            sql_attrs = self.store.sql_attributes_for_datasource(ds["id"])
            if not ok:
                verdict, step = "unreachable", f"Fix the connection ({err}); the catalog can still be read but no SQL can run."
            elif not tables:
                verdict, step = "not_scanned", "Call scan_datasource."
            elif not terms:
                verdict, step = "not_modeled", "Call get_modeling_worklist and save_table_terms for each table."
            elif remaining:
                verdict, step = "partially_modeled", f"{len(remaining)} table(s) still have no Term; continue with get_modeling_worklist."
            elif changed:
                verdict, step = "partially_modeled", (
                    f"{len(changed)} modeled table(s) have columns added since they were modeled; map them with get_modeling_worklist."
                )
            else:
                verdict, step = "ready", "Ask questions: check_answerable → get_query_context → validate_sql → run_sql."
            notes = []
            examples = self.store.examples(ds["id"])
            if verdict == "ready" and ds["name"] != KNOWLEDGE_DS:
                if not sql_attrs and not examples:
                    notes.append(
                        "No SQL attributes or verified example queries yet: every count, total and filter will be "
                        "written from scratch. Before relying on answers, run suggest_sql_attributes and save the "
                        "measures and filters a person agrees with, and save_example_query for confirmed answers."
                    )
                if terms and not any(t["status"] == "certified" for t in terms):
                    notes.append("No Term is certified yet; answers will cite draft Terms until a person certifies them.")
            if ds["name"] == KNOWLEDGE_DS:
                stats = self.knowledge.stats()
                if not stats.get("entities") and not stats.get("concepts"):
                    verdict, step = "empty", "Save what the user shares: define_entity + add_records for facts, save_concept for ideas."
                else:
                    verdict, step = "ready", "Search with datasource='knowledge'; cite sources via _source_id or get_source."
            out.append(
                {
                    "datasource": ds["name"],
                    "dialect": ds.get("dialect"),
                    "url": mask_url(ds["url"]),
                    "description": ds.get("description"),
                    "can_execute_sql": ok,
                    "connection_error": err,
                    "scanned_at": ds.get("scanned_at"),
                    "tables": len(tables),
                    "modeled_tables": len(modeled),
                    "tables_without_terms": [t["name"] for t in remaining][:30],
                    "tables_with_new_columns": [t["name"] for t in changed][:30],
                    "terms": {"total": len(terms), "certified": sum(t["status"] == "certified" for t in terms)},
                    "relationships": {
                        "accepted": sum(r["status"] == "accepted" for r in rels),
                        "proposed": sum(r["status"] == "proposed" for r in rels),
                    },
                    "sql_attributes": len(sql_attrs),
                    "example_queries": len(examples),
                    "verdict": verdict,
                    "next_step": step,
                    **({"notes": notes} if notes else {}),
                    **({"knowledge": self.knowledge.stats()} if ds["name"] == KNOWLEDGE_DS else {}),
                }
            )
        return {
            "server_version": __version__,
            "datasources": out,
            "can_add_datasources": self.settings.allow_datasource_tools,
            "next_step": None if out else (
                "No datasource yet. To keep personal knowledge, start with define_entity or save_concept "
                "(the 'knowledge' store is created on first use). To query a database, call add_datasource(name, url) "
                "or run `auto-ontology-mcp demo` in a terminal."
            ),
        }

    # ============================================================== datasources
    def add_datasource(self, name: str, url: str, description: str | None = None, schemas: list[str] | None = None, scan: bool = True) -> dict[str, Any]:
        if not self.settings.allow_datasource_tools:
            raise OntologyError("Adding datasources over MCP is disabled on this server (AUTO_ONTOLOGY_ALLOW_DATASOURCE_TOOLS=0).")
        name = (name or "").strip()
        if not name or len(name) > 64:
            raise OntologyError("Give the datasource a short name (1-64 characters).")
        try:
            url = normalize_url(url)
        except Exception as exc:  # noqa: BLE001
            raise OntologyError(f"Not a valid SQLAlchemy URL: {short_error(exc)}") from exc
        existing = self.store.datasource_by_name(name)
        ds = {
            "id": existing["id"] if existing else new_id("ds"),
            "name": name,
            "url": url,
            "description": description,
            "schemas": schemas or None,
            "created_at": existing["created_at"] if existing else now(),
        }
        self.connections.forget(ds["id"])
        try:
            engine = self.connections.engine(ds)
            ds["dialect"] = engine.dialect.name
        except ModuleNotFoundError as exc:
            raise OntologyError(
                f"The database driver is not installed ({exc.name}). Install the matching extra, e.g. "
                "`uvx --from 'auto-ontology-mcp[postgres]' …` or `pip install psycopg[binary]`."
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise OntologyError(f"Could not open the datasource: {short_error(exc)}") from exc
        ok, err = self.connections.test(ds)
        if not ok:
            self.connections.forget(ds["id"])
            raise OntologyError(f"Connection test failed: {err}")
        if existing:
            self.store.update("datasource", ds["id"], {k: v for k, v in ds.items() if k != "id"})
        else:
            self.store.insert("datasource", ds)
        self.store.log("datasource", ds["id"], "upsert", {"name": name, "url": mask_url(url)})
        result: dict[str, Any] = {"datasource": name, "dialect": ds["dialect"], "url": mask_url(url), "connected": True}
        if scan:
            result["scan"] = self.scan_datasource(name)
        else:
            result["next_step"] = "Call scan_datasource to read the schema."
        return result

    def scan_datasource(self, datasource: str | None = None, schemas: list[str] | None = None, profile: bool = True) -> dict[str, Any]:
        ds = self.ds(datasource)
        summary = self.scanner.scan(ds, schemas=schemas, profile=profile)
        ds = self.ds(ds["name"])
        gaps = self._modeling_gaps(ds)
        names = {t["id"]: _label(t["schema_name"], t["name"]) for t in gaps["tables"]}
        summary["tables_without_terms"] = len(gaps["without_terms"])
        summary["new_columns_on_modeled_tables"] = {names[tid]: [c["name"] for c in cols] for tid, cols in gaps["new_columns"].items()}
        model = "Model tables with get_modeling_worklist" + (
            " (it also returns modeled tables that gained columns)." if gaps["new_columns"] else "."
        )
        summary["next_step"] = (
            f"Review proposed relationships with list_relationships, then {model[0].lower()}{model[1:]}"
            if summary.get("relationships_proposed")
            else model
        )
        return {"datasource": ds["name"], **summary}

    # ================================================================= modeling
    def _modeled_table_ids(self, ds: dict[str, Any]) -> set[str]:
        ids = {t["primary_table_id"] for t in self.store.terms(ds["id"]) if t.get("primary_table_id")}
        ids |= {a["table_id"] for a in self.store.attributes_for_datasource(ds["id"])}
        return ids

    def _fk_columns(self, ds: dict[str, Any], statuses: tuple[str, ...] = ("accepted",)) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in self.store.relationships(ds["id"], statuses):
            out[r["source_table_id"]].append(r)
        return out

    def _candidate_columns(self, ds: dict[str, Any], table: dict[str, Any], fks: dict[str, list[dict[str, Any]]] | None = None) -> list[dict[str, Any]]:
        fks = fks if fks is not None else self._fk_columns(ds)
        fk_names = {r["source_column"] for r in fks.get(table["id"], [])}
        return [c for c in self.store.columns(table["id"]) if c["name"] not in fk_names]

    def _modeling_gaps(self, ds: dict[str, Any]) -> dict[str, Any]:
        """Tables with no Term yet, and modeled tables with columns no Term maps (added since they were
        modeled: save_table_terms maps every candidate column, so an unmapped one is new)."""
        tables = self.store.tables(ds["id"])
        modeled = self._modeled_table_ids(ds)
        fks = self._fk_columns(ds)
        knowledge = self.knowledge.is_knowledge(ds)  # entities map their own columns through define_entity
        mapped = {a["column_id"] for a in self.store.attributes_for_datasource(ds["id"])}
        without_terms, new_columns = [], {}
        for t in tables:
            cand = self._candidate_columns(ds, t, fks)
            if not cand:
                continue
            if t["id"] not in modeled:
                without_terms.append(t)
            elif not knowledge:
                unmapped = [c for c in cand if c["id"] not in mapped]
                if unmapped:
                    new_columns[t["id"]] = unmapped
        return {"tables": tables, "modeled": modeled, "fks": fks, "without_terms": without_terms, "new_columns": new_columns}

    @staticmethod
    def _column_view(c: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {"column": c["name"], "type": c.get("data_type")}
        if c.get("description"):
            out["description"] = c["description"]
        if c.get("sample_values"):
            out["all_values" if c.get("value_set_complete") else "sample_values"] = c["sample_values"]
        if c.get("is_unique") is not None:
            out["unique_in_sample"] = bool(c["is_unique"])
        if c.get("null_fraction"):
            out["null_fraction"] = c["null_fraction"]
        if c.get("format"):
            out["format"] = c["format"]
        if c.get("min_value") is not None and c.get("max_value") is not None:
            out["range_in_sample"] = [c["min_value"], c["max_value"]]
        return out

    def get_modeling_worklist(self, datasource: str | None = None, limit: int = 3, tables: list[str] | None = None) -> dict[str, Any]:
        ds = self.ds(datasource)
        gaps = self._modeling_gaps(ds)
        all_tables, fks, modeled, new_columns = gaps["tables"], gaps["fks"], gaps["modeled"], gaps["new_columns"]
        if not all_tables:
            raise OntologyError("The datasource has not been scanned yet. Call scan_datasource first.")
        terms = self.store.terms(ds["id"])
        term_by_table = defaultdict(list)
        for t in terms:
            if t.get("primary_table_id"):
                term_by_table[t["primary_table_id"]].append(t["name"])

        if tables:
            todo = [self.table_ref(ds, ref) for ref in tables]
        else:
            pending = [*gaps["without_terms"], *(t for t in all_tables if t["id"] in new_columns)]
            pending.sort(key=lambda t: (t["id"] in new_columns, len(fks.get(t["id"], [])), t["schema_name"], t["name"]))
            todo = pending[: max(1, min(limit, 10))]
        incoming = defaultdict(list)
        for r in self.store.relationships(ds["id"], ("accepted",)):
            incoming[r["target_table_id"]].append(r)

        bridge_only = [
            t["name"] for t in all_tables if t["id"] not in modeled and not self._candidate_columns(ds, t, fks)
        ]
        mapping: dict[str, list[dict[str, Any]]] = defaultdict(list)
        if any(t["id"] in new_columns for t in todo):
            for a in self.store.attributes_for_datasource(ds["id"]):
                mapping[a["term_id"]].append(a)
        items = []
        for t in todo:
            cand = self._candidate_columns(ds, t, fks)
            item: dict[str, Any] = {}
            if t["id"] in new_columns:
                item["new_columns"] = [c["name"] for c in new_columns[t["id"]]]
                item["current_terms"] = [
                    {
                        "name": term["name"],
                        "description": term.get("description"),
                        "synonyms": term.get("synonyms") or [],
                        "columns": [
                            {"column": a["column_name"], "display_name": a["display_name"], **({"description": a["description"]} if a.get("description") else {})}
                            for a in mapping[term["id"]] if a["table_id"] == t["id"]
                        ],
                    }
                    for term in terms if term.get("primary_table_id") == t["id"]
                ]
            items.append(
                {
                    **item,
                    "table": _label(t["schema_name"], t["name"]),
                    "table_type": t.get("table_type"),
                    "row_count": t.get("row_count"),
                    "description": t.get("description"),
                    "primary_key": t.get("pk") or [],
                    "primary_key_inferred": bool(t.get("pk_inferred")),
                    "junction": t.get("junction_reason"),
                    "candidate_columns": [self._column_view(c) for c in cand],
                    "foreign_keys": [
                        {
                            "column": r["source_column"],
                            "references": f"{_label(r['target_schema'], r['target_table'])}.{r['target_column']}",
                            "role": r.get("role"),
                            "target_terms": term_by_table.get(r["target_table_id"], []),
                        }
                        for r in fks.get(t["id"], [])
                    ],
                    "referenced_by": [
                        f"{_label(r['source_schema'], r['source_table'])}.{r['source_column']}" for r in incoming.get(t["id"], [])
                    ],
                    "existing_terms": term_by_table.get(t["id"], []),
                }
            )
        remaining = len(gaps["without_terms"]) + len(new_columns)
        step = "For each table above call save_table_terms(table, terms=[...]). Then call get_modeling_worklist again."
        if any("new_columns" in i for i in items):
            step += (
                " Tables with new_columns are already modeled: send their current_terms back with each new column"
                " added to the Term it belongs to (a Term left out is removed, a column left out goes to the first Term)."
            )
        return {
            "datasource": ds["name"],
            "instructions": guidance.TERM_RULES,
            "progress": {
                "tables": len(all_tables), "modeled": len(modeled), "remaining": remaining,
                "modeled_with_new_columns": len(new_columns),
            },
            "tables": items,
            "tables_with_only_foreign_keys": bridge_only,
            "next_step": step if items else "Every table with candidate columns has Terms. Review relationships and SQL attributes next.",
        }

    def save_table_terms(
        self,
        table: str,
        terms: list[dict[str, Any]],
        column_descriptions: dict[str, str] | None = None,
        is_junction: bool | None = None,
        datasource: str | None = None,
    ) -> dict[str, Any]:
        ds = self.ds(datasource)
        tbl = self.table_ref(ds, table)
        if not terms:
            raise OntologyError("Give at least one Term.")
        columns = {c["name"].casefold(): c for c in self.store.columns(tbl["id"])}
        candidates = {c["name"].casefold() for c in self._candidate_columns(ds, tbl)}
        other_terms = {
            t["name"].casefold(): t for t in self.store.terms(ds["id"]) if t.get("primary_table_id") != tbl["id"]
        }

        assigned: dict[str, str] = {}
        cleaned = []
        problems = []
        for raw in terms:
            name = clean_label(raw.get("name") or "")
            if not name:
                problems.append("A Term has no name.")
                continue
            if len(name) > 80:
                problems.append(f"Term name too long: {name[:40]}…")
            if name.casefold() in other_terms:
                owner = other_terms[name.casefold()]
                problems.append(
                    f"Term {name!r} already exists for table {owner.get('table_name')}. Choose a qualified name "
                    f"(e.g. '{name} ({to_label(tbl['name'])})') or edit that Term with update_term."
                )
            cols = []
            for entry in raw.get("columns") or []:
                key = (entry.get("column") or "").strip().strip('"`').casefold()
                col = columns.get(key)
                if col is None:
                    close = difflib.get_close_matches(key, list(columns), n=3, cutoff=0.5)
                    problems.append(f"Column {entry.get('column')!r} is not in {tbl['name']}." + (f" Did you mean {', '.join(close)}?" if close else ""))
                    continue
                if key in assigned:
                    problems.append(f"Column {col['name']} is assigned to both {assigned[key]!r} and {name!r}; assign it once.")
                    continue
                assigned[key] = name
                cols.append(
                    {
                        "column": col,
                        "display_name": clean_label(entry.get("display_name") or "") or to_label(col["name"]),
                        "description": entry.get("description"),
                    }
                )
            cleaned.append(
                {
                    "name": name,
                    "description": (raw.get("description") or "").strip(),
                    "synonyms": [s.strip() for s in raw.get("synonyms") or [] if s and s.strip()],
                    "columns": cols,
                }
            )
        if len({t["name"].casefold() for t in cleaned}) != len(cleaned):
            problems.append("Two Terms in this call have the same name.")
        if problems:
            raise OntologyError("Nothing was saved. Fix these and call save_table_terms again:\n- " + "\n- ".join(problems))

        auto_assigned = []
        for key in sorted(candidates - set(assigned)):
            col = columns[key]
            cleaned[0]["columns"].append({"column": col, "display_name": to_label(col["name"]), "description": None})
            auto_assigned.append(col["name"])

        existing = {t["name"].casefold(): t for t in self.store.terms(ds["id"]) if t.get("primary_table_id") == tbl["id"]}
        saved, reset_to_draft, kept, removed = [], [], [], []
        with self.store.tx() as con:
            for t in cleaned:
                prev = existing.get(t["name"].casefold())
                if prev:
                    tid = prev["id"]
                    changed = (prev.get("description") or "") != t["description"] or sorted(prev.get("synonyms") or []) != sorted(t["synonyms"])
                    values: dict[str, Any] = {"name": t["name"], "description": t["description"], "synonyms": t["synonyms"], "updated_at": now()}
                    if changed and prev["status"] == "certified":
                        values.update(status="draft", certified_at=None)
                        reset_to_draft.append(t["name"])
                    self.store.update("term", tid, values, con)
                else:
                    tid = new_id("term")
                    self.store.insert(
                        "term",
                        {
                            "id": tid, "datasource_id": ds["id"], "name": t["name"], "description": t["description"],
                            "synonyms": t["synonyms"], "status": "draft", "primary_table_id": tbl["id"],
                            "created_at": now(), "updated_at": now(),
                        },
                        con,
                    )
                t["id"] = tid
                saved.append(t)
            new_names = {t["name"].casefold() for t in cleaned}
            for key, prev in existing.items():
                if key in new_names:
                    continue
                has_sql = con.execute("SELECT COUNT(*) FROM sql_attribute WHERE term_id = ?", (prev["id"],)).fetchone()[0]
                if has_sql:
                    kept.append(prev["name"])
                else:
                    con.execute("DELETE FROM term WHERE id = ?", (prev["id"],))
                    removed.append(prev["name"])
            col_ids = [c["id"] for c in columns.values()]
            marks = ",".join("?" for _ in col_ids)
            term_ids = [t["id"] for t in saved] + [p["id"] for p in existing.values() if p["name"] in kept]
            tmarks = ",".join("?" for _ in term_ids)
            con.execute(f"DELETE FROM column_attribute WHERE column_id IN ({marks}) AND term_id IN ({tmarks})", (*col_ids, *term_ids))
            for t in saved:
                for c in t["columns"]:
                    self.store.insert(
                        "column_attribute",
                        {"id": new_id("attr"), "term_id": t["id"], "column_id": c["column"]["id"], "display_name": c["display_name"], "description": c["description"]},
                        con,
                    )
            for col_name, desc in (column_descriptions or {}).items():
                col = columns.get(col_name.strip().casefold())
                if col and desc:
                    self.store.update("catalog_column", col["id"], {"description": desc.strip()}, con)
            if is_junction is not None:
                self.store.update(
                    "catalog_table", tbl["id"],
                    {"is_junction": bool(is_junction), "junction_reason": "host: confirmed junction" if is_junction else "host: not a junction"},
                    con,
                )
            self.store.log("table", tbl["id"], "save_terms", {"terms": [t["name"] for t in saved], "removed": removed}, con)

        progress = self.get_modeling_worklist(ds["name"], limit=1)
        return {
            "table": _label(tbl["schema_name"], tbl["name"]),
            "saved_terms": [
                {"id": t["id"], "name": t["name"], "attributes": [c["display_name"] for c in t["columns"]]} for t in saved
            ],
            "auto_assigned_columns": auto_assigned,
            "certified_terms_reset_to_draft": reset_to_draft,
            "removed_terms": removed,
            "kept_terms_with_sql_attributes": kept,
            "progress": progress["progress"],
            "next_table": progress["tables"][0]["table"] if progress["tables"] else None,
        }

    # ============================================================ relationships
    def _relationship_view(self, r: dict[str, Any]) -> dict[str, Any]:
        ev = r.get("evidence") or {}
        return {
            "id": r["id"],
            "source": f"{_label(r['source_schema'], r['source_table'])}.{r['source_column']}",
            "target": f"{_label(r['target_schema'], r['target_table'])}.{r['target_column']}",
            "status": r["status"],
            "origin": r["origin"],
            "confidence": r.get("confidence"),
            "role": r.get("role"),
            "is_default": bool(r.get("is_default")),
            "cardinality": r.get("cardinality"),
            "containment": ev.get("containment"),
            "evidence": ev.get("reasons") or [],
            "description": r.get("description"),
        }

    def list_relationships(self, datasource: str | None = None, status: str = "proposed", table: str | None = None) -> dict[str, Any]:
        ds = self.ds(datasource)
        statuses = ("accepted", "proposed", "rejected") if status == "all" else (status,)
        if status not in ("all", "accepted", "proposed", "rejected"):
            raise OntologyError("status must be proposed, accepted, rejected or all.")
        rels = self.store.relationships(ds["id"], statuses)
        if table:
            tbl = self.table_ref(ds, table)
            rels = [r for r in rels if tbl["id"] in (r["source_table_id"], r["target_table_id"])]
        groups = defaultdict(list)
        for r in self.store.relationships(ds["id"], ("accepted",)):
            groups[(r["source_table"], r["target_table"])].append(r)
        roles = [
            {
                "from": s, "to": t,
                "relationships": [f"{r['source_column']} (role: {r.get('role') or 'default'}{', DEFAULT' if r.get('is_default') else ''}) [{r['id']}]" for r in g],
            }
            for (s, t), g in groups.items() if len(g) > 1
        ]
        return {
            "datasource": ds["name"],
            "instructions": guidance.RELATIONSHIP_RULES,
            "relationships": [self._relationship_view(r) for r in rels],
            "multi_role_joins": roles,
            "unlinked_id_columns": self._unlinked_columns(ds),
            "joins_seen_in_queries": self._observed_joins(ds),
        }

    def _observed_joins(self, ds: dict[str, Any]) -> list[dict[str, Any]]:
        """Join predicates used by successful queries that are not relationships yet."""
        sqls = [r["sql"] for r in self.store.all(
            "SELECT sql FROM query_log WHERE datasource_id = ? AND status = 'ok' ORDER BY executed_at DESC LIMIT 300", (ds["id"],)
        )] + [e["sql"] for e in self.store.examples(ds["id"])]
        if not sqls:
            return []
        cols = self.store.columns_for_datasource(ds["id"])
        index = {((c.get("schema_name") or "").casefold(), c["table_name"].casefold(), c["name"].casefold()): c for c in cols}
        known = {
            frozenset({r["source_column_id"], r["target_column_id"]})
            for r in self.store.relationships(ds["id"], ("accepted", "proposed", "rejected"))
        }
        seen: dict[frozenset, dict[str, Any]] = {}
        for sql in sqls:
            try:
                analysis = analyze(sql, cols, self.dialect(ds))
            except Exception:  # noqa: BLE001
                continue
            for a, b, _ in analysis.joins:
                ca = index.get((a.schema.casefold(), a.table.casefold(), a.column.casefold()))
                cb = index.get((b.schema.casefold(), b.table.casefold(), b.column.casefold()))
                if not ca or not cb:
                    continue
                key = frozenset({ca["id"], cb["id"]})
                if key in known:
                    continue
                entry = seen.setdefault(key, {"join": f"{a.table}.{a.column} = {b.table}.{b.column}", "uses": 0})
                entry["uses"] += 1
        return sorted(seen.values(), key=lambda e: -e["uses"])[:20]

    def _unlinked_columns(self, ds: dict[str, Any]) -> list[dict[str, Any]]:
        rels = self.store.relationships(ds["id"], ("accepted", "proposed", "rejected"))
        linked = {r["source_column_id"] for r in rels} | {r["target_column_id"] for r in rels if r["status"] != "rejected"}
        tables = {t["id"]: t for t in self.store.tables(ds["id"])}
        pk_cols = []
        for t in tables.values():
            if len(t.get("pk") or []) == 1:
                for c in self.store.columns(t["id"]):
                    if c["name"] == t["pk"][0]:
                        pk_cols.append((t, c))
        out = []
        for t in tables.values():
            pk = set(t.get("pk") or [])
            for c in self.store.columns(t["id"]):
                if c["id"] in linked or c["name"] in pk or id_base(c["name"]) is None or names_own_table(c["name"], t["name"]):
                    continue
                compatible = []
                lo, hi = _num(c.get("min_value")), _num(c.get("max_value"))
                for pt, pc in pk_cols:
                    if pt["id"] == t["id"] and not any(w in compact(c["name"]) for w in ("parent", "manager", "reports")):
                        continue
                    plo, phi = _num(pc.get("min_value")), _num(pc.get("max_value"))
                    if lo is not None and plo is not None and (lo < plo or hi > phi):
                        continue
                    if (is_numeric_type(c.get("data_type"))) != (is_numeric_type(pc.get("data_type"))):
                        continue
                    compatible.append(f"{_label(pt['schema_name'], pt['name'])}.{pc['name']}")
                out.append(
                    {
                        "column": f"{_label(t['schema_name'], t['name'])}.{c['name']}",
                        "sample_values": c.get("sample_values"),
                        "value_compatible_targets": compatible[:12],
                    }
                )
        return out[:40]

    def set_relationship(
        self,
        action: str,
        relationship_id: str | None = None,
        source: str | None = None,
        target: str | None = None,
        role: str | None = None,
        description: str | None = None,
        is_default: bool | None = None,
        datasource: str | None = None,
    ) -> dict[str, Any]:
        ds = self.ds(datasource)
        action = (action or "").lower()
        if action not in ("accept", "reject", "add", "update", "delete"):
            raise OntologyError("action must be accept, reject, add, update or delete.")
        rel = None
        if action == "add":
            if not source or not target:
                raise OntologyError("add needs source='table.column' and target='table.column'.")
            s, t = self.column_ref(ds, source), self.column_ref(ds, target)
            existing = self.store.one(
                "SELECT id FROM relationship WHERE source_column_id = ? AND target_column_id = ?", (s["id"], t["id"])
            )
            evidence: dict[str, Any] = {"reasons": ["added by the host model"], "reviewed": True}
            v = Validator(dialect=self.dialect(ds), catalog_columns=[], relationships=[], attributes=[], sql_attributes=[], run=lambda q: self._run(ds, q))
            ratio = v._containment({**s}, {**t})
            if ratio is not None:
                evidence["containment"] = round(ratio, 4)
                evidence["reasons"].append(f"{ratio:.0%} of sampled source values exist in the target")
            values = {
                "origin": "manual", "status": "accepted", "confidence": 1.0, "evidence": evidence,
                "role": role, "description": description, "updated_at": now(),
                "cardinality": "one_to_one" if s.get("is_unique") else "many_to_one",
            }
            if existing:
                rid = existing["id"]
                self.store.update("relationship", rid, values)
            else:
                rid = new_id("rel")
                self.store.insert(
                    "relationship",
                    {"id": rid, "datasource_id": ds["id"], "source_column_id": s["id"], "target_column_id": t["id"], "is_default": 1, **values},
                )
            rel = self._rel_by_id(ds, rid)
        else:
            rel = self._find_rel(ds, relationship_id, source, target)
            if action == "delete":
                self.store.all("DELETE FROM relationship WHERE id = ?", (rel["id"],))
                self.store.log("relationship", rel["id"], "delete", self._relationship_view(rel))
                return {"deleted": self._relationship_view(rel)}
            ev = dict(rel.get("evidence") or {})
            ev["reviewed"] = True
            values = {"evidence": ev, "updated_at": now()}
            if action == "accept":
                values["status"] = "accepted"
            elif action == "reject":
                values["status"] = "rejected"
            if role is not None:
                values["role"] = role or None
            if description is not None:
                values["description"] = description
            self.store.update("relationship", rel["id"], values)
            rel = self._rel_by_id(ds, rel["id"])

        if is_default is not None:
            group = [
                r for r in self.store.relationships(ds["id"], ("accepted", "proposed"))
                if r["source_table_id"] == rel["source_table_id"] and r["target_table_id"] == rel["target_table_id"]
            ]
            with self.store.tx() as con:
                for r in group:
                    ev = dict(r.get("evidence") or {})
                    ev["default_reviewed"] = True
                    flag = (r["id"] == rel["id"]) if is_default else (0 if r["id"] == rel["id"] else r.get("is_default"))
                    self.store.update("relationship", r["id"], {"is_default": bool(flag), "evidence": ev}, con)
        self.scanner._refresh_cardinality_and_defaults(ds)
        self.scanner._refresh_junctions(ds, self.store.tables(ds["id"]), {t["id"]: self.store.columns(t["id"]) for t in self.store.tables(ds["id"])})
        rel = self._rel_by_id(ds, rel["id"])
        self.store.log("relationship", rel["id"], action, self._relationship_view(rel))
        return {"relationship": self._relationship_view(rel)}

    def _rel_by_id(self, ds: dict[str, Any], rid: str) -> dict[str, Any]:
        for r in self.store.relationships(ds["id"], ("accepted", "proposed", "rejected")):
            if r["id"] == rid:
                return r
        raise OntologyError(f"No relationship with id {rid!r}.")

    def _find_rel(self, ds: dict[str, Any], rid: str | None, source: str | None, target: str | None) -> dict[str, Any]:
        if rid:
            return self._rel_by_id(ds, rid)
        if source and target:
            s, t = self.column_ref(ds, source), self.column_ref(ds, target)
            for r in self.store.relationships(ds["id"], ("accepted", "proposed", "rejected")):
                if r["source_column_id"] == s["id"] and r["target_column_id"] == t["id"]:
                    return r
            raise OntologyError(f"No relationship {source} → {target}; use action='add' to create it.")
        raise OntologyError("Pass relationship_id, or source and target.")

    # ========================================================== SQL attributes
    def suggest_sql_attributes(self, datasource: str | None = None, term: str | None = None, limit: int = 15) -> dict[str, Any]:
        ds = self.ds(datasource)
        dialect = self.dialect(ds)
        terms = self.store.terms(ds["id"])
        if term:
            terms = [self.term_ref(ds, term)]
        if not terms:
            raise OntologyError("No Terms yet. Model tables first (get_modeling_worklist).")
        existing = {normalize_sql(s["expression"]) for s in self.store.sql_attributes_for_datasource(ds["id"])}
        cols_by_table = {t["id"]: self.store.columns(t["id"]) for t in self.store.tables(ds["id"])}
        tables = {t["id"]: t for t in self.store.tables(ds["id"])}
        candidates: list[dict[str, Any]] = []

        # 1. Expressions mined from executed and verified queries (Auto Ontology mines warehouse history).
        history = [r["sql"] for r in self.store.all("SELECT sql FROM query_log WHERE datasource_id = ? AND status = 'ok' ORDER BY executed_at DESC LIMIT 500", (ds["id"],))]
        history += [e["sql"] for e in self.store.examples(ds["id"])] * 3  # verified queries weigh more
        catalog_cols = self.store.columns_for_datasource(ds["id"])
        usage: dict[tuple[str, str], dict[str, Any]] = {}
        for sql in history:
            try:
                analysis = analyze(sql, catalog_cols, dialect)
            except Exception:  # noqa: BLE001
                continue
            table_names = {t.casefold() for _, t in analysis.tables}
            for expr in extract_expressions(sql, dialect):
                low = expr.casefold()
                for t in terms:
                    tname = (t.get("table_name") or "").casefold()
                    if not tname or tname not in table_names:
                        continue
                    cols = {c["name"].casefold() for c in cols_by_table.get(t["primary_table_id"], [])}
                    if not any(c in low for c in cols):
                        continue
                    key = (t["id"], normalize_sql(expr))
                    if key[1] in existing:
                        continue
                    entry = usage.setdefault(key, {"term": t["name"], "expression": expr, "uses": 0, "source": "query history", "example_sql": sql})
                    entry["uses"] += 1
        candidates.extend(sorted(usage.values(), key=lambda e: -e["uses"]))

        # 2. Bridge tables: one join pattern through each junction.
        rels = self.store.relationships(ds["id"], ("accepted",))
        term_by_table = {t["primary_table_id"]: t for t in self.store.terms(ds["id"]) if t.get("primary_table_id")}
        for tbl in tables.values():
            if not tbl.get("is_junction"):
                continue
            pairs = [r for r in rels if r["source_table_id"] == tbl["id"]]
            if len(pairs) < 2:
                continue
            owner = term_by_table.get(pairs[0]["target_table_id"])
            if term and owner and owner["id"] != terms[0]["id"]:
                continue
            sql = self._bridge_sql(ds, tbl, pairs)
            if normalize_sql(sql) in existing:
                continue
            candidates.append(
                {
                    "term": owner["name"] if owner else None,
                    "expression": sql,
                    "kind_hint": "join",
                    "source": f"bridge table {tbl['name']}",
                    "note": "Name it after the association (e.g. 'Product Tags'); pick the owning Term.",
                }
            )

        # 3. Starter candidates from profiles, useful before any query history exists.
        for t in terms:
            if not t.get("primary_table_id"):
                continue
            tname = t["table_name"]
            added = 0
            for c in cols_by_table.get(t["primary_table_id"], []):
                words = set(w.lower() for w in to_label(c["name"]).split())
                if is_numeric_type(c.get("data_type")) and id_base(c["name"]) is None and words & _MEASURE_WORDS:
                    expr = f"SUM({tname}.{c['name']})"
                    if normalize_sql(expr) not in existing:
                        candidates.append({"term": t["name"], "expression": expr, "kind_hint": "measure", "source": "column profile"})
                        added += 1
                elif c.get("value_set_complete") and c.get("sample_values") and 1 < len(c["sample_values"]) <= 8:
                    for v in c["sample_values"][:4]:
                        if isinstance(v, str):
                            expr = f"{tname}.{c['name']} = '{v}'"
                            if normalize_sql(expr) not in existing:
                                candidates.append({"term": t["name"], "expression": expr, "kind_hint": "filter", "source": "column profile"})
                                added += 1
                if added >= 6:
                    break
        return {
            "datasource": ds["name"],
            "instructions": guidance.SQL_ATTRIBUTE_RULES,
            "candidates": candidates[: max(1, limit)],
            "more_available": max(0, len(candidates) - limit),
            "next_step": "Save the ones that capture business meaning with save_sql_attribute; skip the rest.",
        }

    def _bridge_sql(self, ds: dict[str, Any], bridge: dict[str, Any], pairs: list[dict[str, Any]]) -> str:
        dialect = self.dialect(ds)
        alias_b = bridge["name"]
        select = exp.select("*").from_(qtable(bridge.get("schema_name") or None, bridge["name"]).as_(alias_b))
        used = {alias_b}
        for r in pairs:
            alias = r["target_table"]
            if alias in used:
                alias = f"{r['target_table']}_{r.get('role') or r['source_column']}"
            used.add(alias)
            select = select.join(
                qtable(r.get("target_schema") or None, r["target_table"]).as_(alias),
                on=exp.EQ(
                    this=exp.column(r["source_column"], table=alias_b),
                    expression=exp.column(r["target_column"], table=alias),
                ),
            )
        return select.sql(dialect=dialect)

    def _validate_attribute(self, ds: dict[str, Any], term: dict[str, Any], expression: str, kind: str) -> dict[str, Any]:
        dialect = self.dialect(ds)
        tbl = self.store.table(term["primary_table_id"]) if term.get("primary_table_id") else None
        if kind in ("join", "query"):
            try:
                tree = guard_readonly(expression, dialect)
                guard_sources(tree, self._catalog_tables(ds))
            except SqlGuardError as exc:
                raise OntologyError(f"{kind} attributes must be a full read-only SELECT: {exc}") from exc
            probe = exp.select("*").from_(tree.subquery("_aoq")).limit(1).sql(dialect=dialect)
        else:
            if tbl is None:
                raise OntologyError("This Term has no table, so a fragment cannot be checked. Use kind='query' with a full SELECT.")
            frm = qtable(tbl.get("schema_name") or None, tbl["name"]).as_(tbl["name"]).sql(dialect=dialect)
            if kind == "filter":
                probe = f"SELECT COUNT(*) FROM {frm} WHERE {expression}"
            else:
                probe = f"SELECT {expression} AS value FROM {frm}" + (" LIMIT 1" if kind == "dimension" else "")
            try:
                guard_sources(guard_readonly(probe, dialect), self._catalog_tables(ds))
            except SqlGuardError as exc:
                raise OntologyError(f"Expression is not a valid read-only fragment: {exc}") from exc
        try:
            rows = self._run(ds, probe, max_rows=1)
        except Exception as exc:  # noqa: BLE001
            hint = ""
            if kind not in ("join", "query"):
                hint = f" Fragments run as `{probe[:160]}`; reference columns as {tbl['name']}.<column>. Use kind='query' for expressions that need other tables."
            raise OntologyError(f"The database rejected the expression: {short_error(exc)}.{hint}") from exc
        result: dict[str, Any] = {"ok": True, "checked_at": now(), "probe": probe}
        if kind == "filter" and rows:
            result["matching_rows"] = rows[0][0]
        elif kind == "measure" and rows:
            result["value_over_whole_table"] = rows[0][0]
        return result

    def save_sql_attribute(
        self,
        term: str,
        name: str,
        expression: str | None = None,
        description: str | None = None,
        kind: str = "measure",
        status: str | None = None,
        delete: bool = False,
        datasource: str | None = None,
    ) -> dict[str, Any]:
        ds = self.ds(datasource)
        t = self.term_ref(ds, term)
        name = clean_label(name)
        prev = self.store.one("SELECT * FROM sql_attribute WHERE term_id = ? AND lower(name) = lower(?)", (t["id"], name))
        if delete:
            if not prev:
                raise OntologyError(f"Term {t['name']} has no SQL attribute {name!r}.")
            self.store.all("DELETE FROM sql_attribute WHERE id = ?", (prev["id"],))
            self.store.log("sql_attribute", prev["id"], "delete", {"term": t["name"], "name": name})
            return {"deleted": {"term": t["name"], "name": name}}
        kind = (kind or "measure").lower()
        if kind not in SQL_ATTR_KINDS:
            raise OntologyError(f"kind must be one of {', '.join(SQL_ATTR_KINDS)}.")
        if status and status not in TERM_STATUSES:
            raise OntologyError(f"status must be one of {', '.join(TERM_STATUSES)}.")
        expression = (expression or (prev or {}).get("expression") or "").strip().rstrip(";")
        if not expression:
            raise OntologyError("expression is required.")
        validation = self._validate_attribute(ds, t, expression, kind)
        stamp = now()
        if prev:
            changed = prev["expression"] != expression or (description is not None and description != prev.get("description")) or prev["kind"] != kind
            values: dict[str, Any] = {"expression": expression, "kind": kind, "validation": validation, "updated_at": stamp}
            if description is not None:
                values["description"] = description
            new_status = status or ("draft" if changed and prev["status"] == "certified" else prev["status"])
            values["status"] = new_status
            values["certified_at"] = stamp if new_status == "certified" and prev["status"] != "certified" else (prev.get("certified_at") if new_status == "certified" else None)
            self.store.update("sql_attribute", prev["id"], values)
            sid = prev["id"]
        else:
            sid = new_id("sqla")
            self.store.insert(
                "sql_attribute",
                {
                    "id": sid, "term_id": t["id"], "name": name, "description": description or "", "expression": expression,
                    "kind": kind, "source": "host", "status": status or "draft", "validation": validation,
                    "created_at": stamp, "updated_at": stamp, "certified_at": stamp if status == "certified" else None,
                },
            )
        self.store.log("sql_attribute", sid, "upsert", {"term": t["name"], "name": name, "expression": expression})
        saved = self.store.one("SELECT * FROM sql_attribute WHERE id = ?", (sid,))
        return {"term": t["name"], "sql_attribute": _sql_attr_view(saved)}

    # ==================================================================== terms
    def update_term(
        self,
        term: str,
        name: str | None = None,
        description: str | None = None,
        synonyms: list[str] | None = None,
        add_synonyms: list[str] | None = None,
        status: str | None = None,
        note: str | None = None,
        column_updates: list[dict[str, Any]] | None = None,
        delete: bool = False,
        datasource: str | None = None,
    ) -> dict[str, Any]:
        ds = self.ds(datasource)
        t = self.term_ref(ds, term)
        if self.knowledge.is_knowledge(ds) and is_builtin(t):
            raise OntologyError(f"{t['name']} is built into the knowledge store and cannot be changed.")
        if delete:
            if t.get("spec"):
                raise OntologyError(f"{t['name']} stores records; delete it with define_entity(name, delete=true).")
            self.store.all("DELETE FROM term WHERE id = ?", (t["id"],))
            self.store.log("term", t["id"], "delete", {"name": t["name"]})
            return {"deleted": t["name"]}
        values: dict[str, Any] = {}
        meaning_changed = False
        if name:
            new_name = clean_label(name)
            clash = self.store.one("SELECT id FROM term WHERE datasource_id = ? AND lower(name) = lower(?) AND id != ?", (ds["id"], new_name, t["id"]))
            if clash:
                raise OntologyError(f"Another Term is already named {new_name!r}.")
            if new_name != t["name"]:
                values["name"] = new_name
                meaning_changed = True
        if description is not None and description != (t.get("description") or ""):
            values["description"] = description
            meaning_changed = True
        syns = list(t.get("synonyms") or [])
        if synonyms is not None:
            syns = [s.strip() for s in synonyms if s and s.strip()]
        for s in add_synonyms or []:
            if s and s.strip() and s.strip().casefold() not in {x.casefold() for x in syns}:
                syns.append(s.strip())
        if syns != (t.get("synonyms") or []):
            values["synonyms"] = syns
        if note is not None:
            values["note"] = note
        if status:
            if status not in TERM_STATUSES:
                raise OntologyError(f"status must be one of {', '.join(TERM_STATUSES)}.")
            values["status"] = status
            values["certified_at"] = now() if status == "certified" else None
        elif meaning_changed and t["status"] == "certified":
            values["status"] = "draft"
            values["certified_at"] = None
        changed_attrs = []
        if column_updates:
            attrs = {a["column_name"].casefold(): a for a in self.store.term_attributes(t["id"])}
            for cu in column_updates:
                a = attrs.get((cu.get("column") or "").casefold())
                if not a:
                    raise OntologyError(f"Term {t['name']} has no column {cu.get('column')!r}. Its columns: {', '.join(a['column_name'] for a in attrs.values())}.")
                upd = {}
                if cu.get("display_name"):
                    upd["display_name"] = clean_label(cu["display_name"])
                if cu.get("description") is not None:
                    upd["description"] = cu["description"]
                if upd:
                    self.store.update("column_attribute", a["id"], upd)
                    changed_attrs.append(a["column_name"])
        if values:
            values["updated_at"] = now()
            self.store.update("term", t["id"], values)
        self.store.log("term", t["id"], "update", {k: v for k, v in values.items() if k != "updated_at"} | ({"columns": changed_attrs} if changed_attrs else {}))
        return {"term": self.get_term(t["id"], datasource=ds["name"], brief=True)}

    def save_glossary(self, entries: list[dict[str, str]] | None = None, delete: list[str] | None = None, datasource: str | None = None, global_scope: bool = False) -> dict[str, Any]:
        ds_id = None if global_scope else self.ds(datasource)["id"]
        saved, removed = [], []
        with self.store.tx() as con:
            for e in entries or []:
                name, definition = (e.get("name") or "").strip(), (e.get("definition") or "").strip()
                if not name or not definition:
                    raise OntologyError("Each glossary entry needs a name and a definition.")
                row = con.execute("SELECT id FROM glossary WHERE COALESCE(datasource_id, '') = ? AND lower(name) = lower(?)", (ds_id or "", name)).fetchone()
                if row:
                    con.execute("UPDATE glossary SET definition = ?, updated_at = ? WHERE id = ?", (definition, now(), row["id"]))
                else:
                    self.store.insert("glossary", {"id": new_id("gls"), "datasource_id": ds_id, "name": name, "definition": definition, "updated_at": now()}, con)
                saved.append(name)
            for name in delete or []:
                cur = con.execute("DELETE FROM glossary WHERE COALESCE(datasource_id, '') = ? AND lower(name) = lower(?)", (ds_id or "", name))
                if cur.rowcount:
                    removed.append(name)
        return {"saved": saved, "deleted": removed, "glossary": [{"name": g["name"], "definition": g["definition"]} for g in self.store.glossary(ds_id)]}

    # ================================================================== reading
    def _docs(self, ds: dict[str, Any]):
        relations = self.store.all(
            """
            SELECT r.source_term_id, r.relation, t.name AS target_name FROM term_relation r
            JOIN term t ON t.id = r.target_term_id WHERE r.datasource_id = ?
            """,
            (ds["id"],),
        )
        return build_docs(
            self.store.terms(ds["id"]),
            self.store.attributes_for_datasource(ds["id"]),
            self.store.sql_attributes_for_datasource(ds["id"]),
            self.store.glossary(ds["id"]),
            [t for t in self.store.tables(ds["id"]) if t["name"] != SOURCES_TABLE],
            self.store.examples(ds["id"]),
            relations=relations,
            sources=self.knowledge.sources_for_search() if ds["name"] == KNOWLEDGE_DS else [],
        )

    def _search(self, ds: dict[str, Any], query: str, kinds: list[str] | None = None, limit: int = 10, docs=None) -> list[dict[str, Any]]:
        return search(query, docs if docs is not None else self._docs(ds), kinds=kinds, limit=limit)

    def search_ontology(self, query: str, kinds: list[str] | None = None, limit: int = 10, datasource: str | None = None) -> dict[str, Any]:
        ds = self.ds(datasource)
        hits = self._search(ds, query, kinds=[k for k in kinds if k != "value"] if kinds else None, limit=max(1, min(limit, 50)))
        # Records are searched by their stored text in the knowledge store (and elsewhere on request).
        if (kinds is None and self.knowledge.is_knowledge(ds)) or (kinds and "value" in kinds):
            for v in self._value_hits(ds, query, [40]):
                hits.append({"kind": "value", "term": v["term"], "attribute": v["attribute"], "column": v["column"], "values": v["values"]})
        out: dict[str, Any] = {"datasource": ds["name"], "query": query, "hits": hits}
        if kinds is None and self.knowledge.is_knowledge(ds):
            try:
                text = self.knowledge.search_text(query, limit=5)
            except KnowledgeError:
                text = {"hits": []}
            if text["hits"]:
                out["text_hits"] = text["hits"]
                out["text_hint"] = "Passages, bodies and sources that mention these words; search_text returns more, with filters."
        out["hint"] = None if hits or out.get("text_hits") else (
            "Nothing matched lexically. Try the schema's own words, a synonym, singular/plural, or translate the phrase."
        )
        return out

    def get_term(self, term: str, datasource: str | None = None, brief: bool = False) -> dict[str, Any]:
        ds = self.ds(datasource)
        t = self.term_ref(ds, term)
        attrs = self.store.term_attributes(t["id"])
        out: dict[str, Any] = {
            "id": t["id"],
            "name": t["name"],
            "status": t["status"],
            "description": t.get("description"),
            "synonyms": t.get("synonyms") or [],
            "table": _label(t.get("schema_name"), t["table_name"]) if t.get("table_name") else None,
            "note": t.get("note"),
            "certified_at": t.get("certified_at"),
            "kind": t.get("kind") or "entity",
        }
        if t.get("body"):
            out["body"] = t["body"]
        if brief:
            out["attributes"] = [a["display_name"] for a in attrs]
            return out
        out["attributes"] = [
            {"name": a["display_name"], **self._column_view({**a, "name": a["column_name"], "description": a.get("description")})}
            for a in attrs
        ]
        out["sql_attributes"] = [_sql_attr_view(s) for s in self.store.sql_attributes(t["id"])]
        out["related_terms"] = self._related_terms(ds, t)
        out["relations"] = self.knowledge.relations_view(t["id"])
        sources = self.knowledge.term_sources(t["id"])
        if sources:
            out["sources"] = sources
        if t.get("spec"):
            out["knowledge"] = self.knowledge.entity_stats(t)
        return out

    def _related_terms(self, ds: dict[str, Any], t: dict[str, Any]) -> list[dict[str, Any]]:
        if not t.get("primary_table_id"):
            return []
        term_by_table = defaultdict(list)
        for x in self.store.terms(ds["id"]):
            if x.get("primary_table_id"):
                term_by_table[x["primary_table_id"]].append(x["name"])
        related = []
        for r in self.store.relationships(ds["id"], ("accepted",)):
            if r["source_table_id"] == t["primary_table_id"] and r["target_table_id"] != r["source_table_id"]:
                other, direction = r["target_table_id"], "references"
            elif r["target_table_id"] == t["primary_table_id"] and r["source_table_id"] != r["target_table_id"]:
                other, direction = r["source_table_id"], "referenced_by"
            elif r["source_table_id"] == r["target_table_id"] == t["primary_table_id"]:
                other, direction = r["source_table_id"], "self_reference"
            else:
                continue
            related.append(
                {
                    "terms": term_by_table.get(other, []),
                    "direction": direction,
                    "join": f"{r['source_table']}.{r['source_column']} = {r['target_table']}.{r['target_column']}",
                    "multiplicity": ("many-to-one" if direction == "references" else "one-to-many") if r.get("cardinality") != "one_to_one" else "one-to-one",
                    "role": r.get("role"),
                    "is_default": bool(r.get("is_default")),
                }
            )
        return related

    def list_terms(
        self, datasource: str | None = None, kind: str = "all", status: str | None = None, limit: int = 200, offset: int = 0
    ) -> dict[str, Any]:
        """The index: every Term with its one-line definition, like an LLM Wiki's index.md."""
        ds = self.ds(datasource)
        if kind not in ("all", "entity", "concept"):
            raise OntologyError("kind must be all, entity or concept.")
        terms = self.store.terms(ds["id"])
        rel_counts: dict[str, int] = defaultdict(int)
        for r in self.store.all("SELECT source_term_id, target_term_id FROM term_relation WHERE datasource_id = ?", (ds["id"],)):
            rel_counts[r["source_term_id"]] += 1
            rel_counts[r["target_term_id"]] += 1
        rows = []
        built_in = []
        for t in terms:
            tkind = t.get("kind") or "entity"
            if self.knowledge.is_knowledge(ds) and is_builtin(t):
                built_in.append(f"{t['name']} ({t['table_name']})" if t.get("table_name") else t["name"])
                continue
            if kind != "all" and tkind != kind:
                continue
            if status and t["status"] != status:
                continue
            item: dict[str, Any] = {
                "name": t["name"],
                "kind": tkind,
                "status": t["status"],
                "description": (t.get("description") or "")[:200],
            }
            if t.get("synonyms"):
                item["synonyms"] = t["synonyms"]
            if t.get("table_name"):
                item["table"] = _label(t.get("schema_name"), t["table_name"])
            if t.get("spec"):
                item["records"] = self.knowledge.entity_stats(t).get("records", 0)
            if rel_counts.get(t["id"]):
                item["relations"] = rel_counts[t["id"]]
            if tkind == "concept" and not (t.get("description") or t.get("body")):
                item["empty"] = True
            rows.append(item)
        page = rows[offset: offset + max(1, min(limit, 500))]
        out = {"datasource": ds["name"], "total": len(rows), "offset": offset, "terms": page}
        if built_in:
            out["built_in_tables"] = built_in
            out["built_in_note"] = "Tables the store keeps for you (sources, passages, the Term mirror and search indexes); query them with SQL."
        return out

    def recent_changes(self, limit: int = 30) -> dict[str, Any]:
        """What was added or changed lately, newest first, like an LLM Wiki's log.md."""
        rows = self.store.all(
            "SELECT * FROM changelog WHERE NOT (entity_type = 'datasource' AND action = 'scan') ORDER BY seq DESC LIMIT ?",
            (max(1, min(limit, 500)),),
        )
        names = {t["id"]: t["name"] for t in self.store.all("SELECT id, name FROM term")}
        changes = []
        for r in rows:
            d = r.get("detail") or {}
            subject = names.get(r.get("entity_id") or "", "") or d.get("name") or d.get("question") or d.get("table") or r.get("entity_id") or ""
            if r["entity_type"] == "knowledge_records":
                counts = ", ".join(f"{v} {k}" for k, v in d.items() if isinstance(v, int) and v) if r["action"] == "add" else f"ids {d.get('ids')}"
                what = f"{subject}: records {r['action']} ({counts or 'no change'})"
            elif r["entity_type"] == "sql_attribute":
                what = f"SQL attribute {d.get('term', '')} › {d.get('name', subject)}: {r['action']}"
            elif r["entity_type"] == "relationship":
                what = f"relationship {d.get('source', '')} → {d.get('target', '')}: {r['action']}"
            elif r["entity_type"] == "source" and r["action"] == "replace_original":
                what = f"source {d.get('title') or subject}: new original {d.get('original')} (previous {d.get('previous')} kept)"
            else:
                what = f"{r['entity_type'].replace('_', ' ')} {subject}: {r['action']}"
            changes.append({"at": r["at"], "what": what})
        return {"changes": changes}

    def export_wiki(self, datasource: str | None = None, directory: str | None = None) -> dict[str, Any]:
        from pathlib import Path

        from .wiki import export_wiki

        if datasource is None and self.knowledge.ds(create=False) is not None:
            datasource = KNOWLEDGE_DS
        ds = self.ds(datasource)
        if self.knowledge.is_knowledge(ds):
            self.knowledge.index.sync()
            default = self.knowledge.wiki_dir
        else:
            default = self.settings.home / "wiki" / ds["name"]
        target = Path(directory).expanduser() if directory else default
        return export_wiki(self, ds, target)

    def describe_table(self, table: str, datasource: str | None = None) -> dict[str, Any]:
        ds = self.ds(datasource)
        tbl = self.table_ref(ds, table)
        attrs = defaultdict(list)
        for a in self.store.attributes_for_datasource(ds["id"]):
            if a["table_id"] == tbl["id"]:
                attrs[a["column_id"]].append(f"{a['term_name']} › {a['display_name']}")
        rels = [self._relationship_view(r) for r in self.store.relationships(ds["id"], ("accepted", "proposed")) if tbl["id"] in (r["source_table_id"], r["target_table_id"])]
        terms = [t for t in self.store.terms(ds["id"]) if t.get("primary_table_id") == tbl["id"]]
        return {
            "table": _label(tbl["schema_name"], tbl["name"]),
            "qualified_name": self._qualified(ds, tbl),
            "table_type": tbl.get("table_type"),
            "row_count": tbl.get("row_count"),
            "description": tbl.get("description"),
            "primary_key": tbl.get("pk") or [],
            "primary_key_inferred": bool(tbl.get("pk_inferred")),
            "junction": tbl.get("junction_reason"),
            "terms": [{"name": t["name"], "status": t["status"], "description": t.get("description")} for t in terms],
            "columns": [{**self._column_view(c), "attributes": attrs.get(c["id"], [])} for c in self.store.columns(tbl["id"])],
            "relationships": rels,
            "sql_attributes": [
                {"term": t["name"], **_sql_attr_view(s)} for t in terms for s in self.store.sql_attributes(t["id"])
            ],
        }

    def _graph(self, ds: dict[str, Any]) -> JoinGraph:
        return JoinGraph(self.store.relationships(ds["id"], ("accepted",)), self.store.tables(ds["id"]))

    def _table_of(self, ds: dict[str, Any], ref: str) -> dict[str, Any]:
        """Resolve a Term name or a table name to a catalog table."""
        try:
            t = self.term_ref(ds, ref)
            if t.get("primary_table_id"):
                return self.store.table(t["primary_table_id"])  # type: ignore[return-value]
        except OntologyError:
            pass
        return self.table_ref(ds, ref)

    def _render_join(self, ds: dict[str, Any], hops: list[dict[str, Any]]) -> str | None:
        if not hops:
            return None
        dialect = self.dialect(ds)
        tables = {_label(t["schema_name"], t["name"]): t for t in self.store.tables(ds["id"])}
        first = tables[hops[0]["from"]]
        select = exp.select("*").from_(qtable(first.get("schema_name") or None, first["name"]).as_(first["name"]))
        joined = {hops[0]["from"]}
        for hop in hops:
            target = hop["to"] if hop["to"] not in joined else hop["from"]
            if target in joined:
                continue
            t = tables[target]
            select = select.join(qtable(t.get("schema_name") or None, t["name"]).as_(t["name"]), on=hop["on"])
            joined.add(target)
        text = select.sql(dialect=dialect, pretty=True)
        idx = text.find("FROM ")
        return text[idx:] if idx >= 0 else text

    def find_join_path(self, source: str, target: str, datasource: str | None = None) -> dict[str, Any]:
        ds = self.ds(datasource)
        a, b = self._table_of(ds, source), self._table_of(ds, target)
        g = self._graph(ds)
        hops = g.path(a["id"], b["id"])
        if hops is None:
            return {
                "found": False,
                "message": f"No chain of accepted relationships connects {a['name']} and {b['name']}. "
                "Check list_relationships for proposed or unlinked columns.",
            }
        alternatives = []
        for hop in hops:
            ta = next(t for t in g.tables.values() if _label(t["schema_name"], t["name"]) == hop["from"])
            tb = next(t for t in g.tables.values() if _label(t["schema_name"], t["name"]) == hop["to"])
            alts = [x for x in g.alternatives(ta["id"], tb["id"]) if x["relationship_id"] != hop["relationship_id"]]
            if alts:
                alternatives.append({"between": f"{hop['from']} and {hop['to']}", "chosen": hop["on"], "other_roles": [f"{x['on']} (role: {x['role']})" for x in alts]})
        return {
            "found": True,
            "from": _label(a["schema_name"], a["name"]),
            "to": _label(b["schema_name"], b["name"]),
            "hops": hops,
            "join_sql": self._render_join(ds, hops),
            "fan_out_warning": [f"{h['from']} → {h['to']} is one-to-many: rows of {h['from']} repeat." for h in hops if h["fans_out"]],
            "shared_parent_warning": [_shared_parent_note(s) for s in shared_parents(hops)],
            "role_alternatives": alternatives,
        }

    # ============================================================== answering
    def check_answerable(self, question: str, entities: list[str] | None = None, datasource: str | None = None) -> dict[str, Any]:
        ds = self.ds(datasource)
        docs = self._docs(ds)
        if not entities:
            from .naming import tokens

            entities = list(dict.fromkeys(tokens(question)))[:6]
        results = []
        covered = 0.0
        value_budget = [60]  # columns probed across the whole question
        for ent in entities:
            hits = self._search(ds, ent, kinds=["term", "attribute", "sql_attribute", "glossary", "table"], limit=3, docs=docs)
            top = hits[0] if hits else None
            entry: dict[str, Any] = {"entity": ent}
            if top and top["kind"] != "table" and top["score"] >= 0.55:
                state = "covered"
                covered += 1
            else:
                # Names of things (a product, a place, a status) live in the data, not the schema.
                values = self._value_hits(ds, ent, value_budget)
                if values:
                    state = "covered_by_value"
                    covered += 1
                    entry["value_matches"] = values
                elif top and (top["score"] >= 0.35 or top["kind"] == "table"):
                    state = "weak" if top["kind"] != "table" else "unmodeled_table"
                    covered += 0.5
                else:
                    state = "not_covered"
            entry.update(state=state, matches=hits)
            results.append(entry)
        coverage = round(covered / len(entities), 2) if entities else 0.0
        missing = [r["entity"] for r in results if r["state"] == "not_covered"]
        weak = [r["entity"] for r in results if r["state"] in ("weak", "unmodeled_table")]
        if not missing and not weak:
            verdict, advice = "answerable", (
                "Call get_query_context with the matched Terms. Entities covered by value are filter literals: "
                "use the exact stored values shown in value_matches."
            )
        elif missing and len(missing) == len(entities):
            verdict, advice = "not_answerable", "Nothing in the ontology covers this question. Tell the user what data would be needed."
        elif missing:
            verdict, advice = "partially_answerable", (
                f"No Term covers: {', '.join(missing)}. Do not substitute a similar measure; tell the user what is missing "
                "and offer what can be answered, or search again with other words first."
            )
        else:
            verdict, advice = "uncertain", f"Weak matches for: {', '.join(weak)}. Check them with get_term or search_ontology before writing SQL."
        # Every entity having a Term is not enough: the Terms' tables must also be linked.
        links = self._entity_links(ds, results)
        unlinked = [x for x in links if x["state"] != "linked"]
        if unlinked and verdict == "answerable":
            verdict = "uncertain"
            advice = " ".join(x["note"] for x in unlinked) + " " + advice
        concepts = sorted({
            m["name"] for r in results if r["state"] == "covered" for m in r["matches"][:1]
            if m["kind"] == "term" and m.get("term_kind") == "concept"
        })
        if concepts:
            advice += (
                f" Concepts matched ({', '.join(concepts)}): answer 'what is / what do I know about' questions from "
                "get_term (definition, body, relations, sources) rather than SQL."
            )
        return {
            "datasource": ds["name"],
            "question": question,
            "coverage": coverage,
            "verdict": verdict,
            "entities": results,
            "links": links,
            "next_step": advice,
            "entity_rules": None if entities else guidance.ENTITY_RULES,
        }

    def _entity_links(self, ds: dict[str, Any], results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """How the tables behind the matched entities connect: linked, only through a shared parent, or not at all."""
        tables: dict[str, dict[str, Any]] = {}
        for r in results:
            ref = None
            if r["state"] == "covered" and r["matches"]:
                top = r["matches"][0]
                ref = top["name"] if top["kind"] == "term" else top.get("term")
            elif r["state"] == "covered_by_value" and r.get("value_matches"):
                ref = r["value_matches"][0].get("term")
            if not ref:
                continue
            try:
                term = self.term_ref(ds, ref)
            except OntologyError:
                continue
            tid = term.get("primary_table_id")
            if tid and tid not in tables:
                tables[tid] = {"term": term["name"], "entity": r["entity"]}
        if len(tables) < 2:
            return []
        g = self._graph(ds)
        ids = list(tables)
        links = []
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                ta, tb = tables[a]["term"], tables[b]["term"]
                hops = g.path(a, b)
                if hops is None:
                    links.append({
                        "between": [ta, tb],
                        "state": "not_linked",
                        "note": f"No chain of relationships links {ta} and {tb}: they cannot be combined row by row. "
                        "Answer each part on its own, or tell the user the data does not link them.",
                    })
                    continue
                shared = shared_parents(hops)
                if shared:
                    through = shared[0]["parent"]
                    links.append({
                        "between": [ta, tb],
                        "state": "only_through_shared_parent",
                        "through": through,
                        "path": [h["on"] for h in hops],
                        "note": f"{ta} and {tb} meet only at {through}: the path pairs every {ta} row with every "
                        f"{tb} row of the same {through} row. That fits a question about pairs or a grouping by one side, "
                        f"but nothing says which {ta} row became or belongs to which {tb} row. If the question needs "
                        "that, add a condition that ties the two rows (for example the same person) or say the data "
                        "has no such link.",
                    })
                else:
                    links.append({"between": [ta, tb], "state": "linked", "path": [h["on"] for h in hops]})
        return links

    def _value_hits(self, ds: dict[str, Any], phrase: str, budget: list[int]) -> list[dict[str, Any]]:
        """Text columns of modeled Terms whose stored values contain every word of *phrase*."""
        import re

        from .naming import STOPWORDS

        words = [w for w in re.findall(r"\w+", phrase.casefold()) if len(w) >= 2 and w not in STOPWORDS]
        if not words or len(words) > 8:
            return []
        dialect = self.dialect(ds)
        found: list[dict[str, Any]] = []
        candidates = []
        for a in self.store.attributes_for_datasource(ds["id"]):
            dtype = (a.get("data_type") or "").lower()
            if a.get("format") or id_base(a["column_name"]) is not None:
                continue
            if not any(k in dtype for k in ("char", "text", "string", "clob", "enum")) and dtype:
                continue
            candidates.append(a)
        # Cheap pass over stored samples first, then live probes.
        for a in candidates:
            samples = a.get("sample_values") if isinstance(a.get("sample_values"), list) else []
            matched = [v for v in samples if isinstance(v, str) and all(w in v.casefold() for w in words)]
            if matched:
                found.append({"term": a["term_name"], "attribute": a["display_name"], "column": f"{a['table_name']}.{a['column_name']}", "values": matched[:3]})
        if found:
            return found[:3]
        candidates.sort(key=lambda a: (0 if a.get("value_set_complete") else 1, 0 if "name" in a["column_name"].lower() else 1))
        for a in candidates:
            if budget[0] <= 0 or len(found) >= 3:
                break
            budget[0] -= 1
            col = exp.column(exp.to_identifier(a["column_name"], quoted=True))
            cond = None
            for w in words:
                like = exp.Like(this=exp.Lower(this=exp.cast(col.copy(), "TEXT")), expression=exp.Literal.string(f"%{w}%"))
                cond = like if cond is None else exp.and_(cond, like)
            q = exp.select(col).distinct().from_(qtable(a.get("schema_name") or None, a["table_name"])).where(cond).limit(3)
            try:
                rows = self._run(ds, q.sql(dialect=dialect), max_rows=3)
            except Exception:  # noqa: BLE001
                continue
            if rows:
                found.append({"term": a["term_name"], "attribute": a["display_name"], "column": f"{a['table_name']}.{a['column_name']}", "values": [r[0] for r in rows]})
        return found

    def get_query_context(self, terms: list[str], question: str | None = None, datasource: str | None = None) -> dict[str, Any]:
        ds = self.ds(datasource)
        if self.knowledge.is_knowledge(ds):
            self.knowledge.index.sync()
        dialect = self.dialect(ds)
        resolved, unresolved = [], []
        for ref in terms:
            try:
                resolved.append(self.term_ref(ds, ref))
            except OntologyError as exc:
                unresolved.append(str(exc))
        if not resolved:
            raise OntologyError("None of the Terms resolved. " + " ".join(unresolved))
        seen_ids: set[str] = set()
        term_views = []
        table_ids: list[str] = []
        for t in resolved:
            if t["id"] in seen_ids:
                continue
            seen_ids.add(t["id"])
            view = self.get_term(t["id"], datasource=ds["name"])
            view.pop("related_terms", None)
            if t.get("primary_table_id"):
                tbl = self.store.table(t["primary_table_id"])
                view["qualified_table"] = self._qualified(ds, tbl)
                if t["primary_table_id"] not in table_ids:
                    table_ids.append(t["primary_table_id"])
            term_views.append(view)

        g = self._graph(ds)
        hops, unreachable = g.connect(table_ids)
        role_notes = []
        for hop in hops:
            ids = [k for k, v in g.tables.items() if _label(v["schema_name"], v["name"]) in (hop["from"], hop["to"])]
            if len(ids) == 2:
                alts = [x for x in g.alternatives(ids[0], ids[1]) if x["relationship_id"] != hop["relationship_id"]]
                if alts:
                    role_notes.append(f"{hop['on']} is the default; other roles: " + "; ".join(f"{x['on']} ({x['role']})" for x in alts))

        examples = []
        all_examples = self.store.examples(ds["id"])
        if question and all_examples:
            ex_docs = [d for d in self._docs(ds) if d.kind == "example"]
            for hit in self._search(ds, question, kinds=["example"], limit=3, docs=ex_docs):
                if hit["score"] >= 0.35:
                    examples.append({"question": hit["name"], "sql": hit["sql"], "score": hit["score"]})
        if len(examples) < 3:
            names = {g.tables[tid]["name"].casefold() for tid in table_ids}
            for e in all_examples:
                if any(n in e["sql"].casefold() for n in names) and all(x["question"] != e["question"] for x in examples):
                    examples.append({"question": e["question"], "sql": e["sql"]})
                if len(examples) >= 3:
                    break
        glossary = self.store.glossary(ds["id"])
        return {
            "datasource": ds["name"],
            "dialect": dialect or ds.get("dialect"),
            "terms": term_views,
            "join_path": {
                "hops": hops,
                "join_sql": self._render_join(ds, hops),
                "role_notes": role_notes,
                "shared_parent_notes": [_shared_parent_note(s) for s in shared_parents(hops)],
                "unreachable_terms": [g.tables[t]["name"] for t in unreachable],
            },
            "verified_examples": examples,
            "glossary": [{"name": x["name"], "definition": x["definition"]} for x in glossary[:30]],
            "unresolved": unresolved,
            "sql_rules": guidance.SQL_RULES,
            "next_step": "Write the SQL, check literals with lookup_values if unsure, then validate_sql and run_sql.",
        }

    def lookup_values(self, column: str, contains: str | None = None, limit: int = 20, datasource: str | None = None) -> dict[str, Any]:
        ds = self.ds(datasource)
        col = self.column_ref(ds, column)
        dialect = self.dialect(ds)
        table = qtable(col.get("schema_name") or None, col["table_name"])
        c = exp.column(exp.to_identifier(col["name"], quoted=True))
        limit = max(1, min(limit, 100))
        if contains:
            cond = exp.Like(this=exp.Lower(this=exp.cast(c, "TEXT")), expression=exp.Literal.string(f"%{contains.strip().lower()}%"))
            q = exp.select(c, exp.Count(this=exp.Star()).as_("n")).from_(table).where(cond).group_by(c).order_by(exp.Ordered(this=exp.column("n"), desc=True)).limit(limit)
            rows = self._run(ds, q.sql(dialect=dialect), max_rows=limit)
            matches = [{"value": r[0], "rows": r[1]} for r in rows]
            close: list[Any] = []
            if not matches:
                q2 = exp.select(c).distinct().from_(table).where(exp.Not(this=exp.Is(this=c, expression=exp.Null()))).limit(2000)
                values = [r[0] for r in self._run(ds, q2.sql(dialect=dialect), max_rows=2000) if isinstance(r[0], str)]
                close = difflib.get_close_matches(contains, values, n=8, cutoff=0.5)
            return {"column": f"{col['table_name']}.{col['name']}", "contains": contains, "matches": matches, "close_values": close}
        q = exp.select(c, exp.Count(this=exp.Star()).as_("n")).from_(table).group_by(c).order_by(exp.Ordered(this=exp.column("n"), desc=True)).limit(limit + 1)
        rows = self._run(ds, q.sql(dialect=dialect), max_rows=limit + 1)
        return {
            "column": f"{col['table_name']}.{col['name']}",
            "top_values": [{"value": r[0], "rows": r[1]} for r in rows[:limit]],
            "more_values_exist": len(rows) > limit,
        }

    def _validator(self, ds: dict[str, Any]) -> Validator:
        columns = self.store.columns_for_datasource(ds["id"])
        if self.knowledge.is_knowledge(ds):
            columns = columns + self.knowledge.index.pseudo_columns(columns)
        return Validator(
            dialect=self.dialect(ds),
            catalog_columns=columns,
            relationships=self.store.relationships(ds["id"], ("accepted", "proposed", "rejected")),
            attributes=self.store.attributes_for_datasource(ds["id"]),
            sql_attributes=self.store.sql_attributes_for_datasource(ds["id"]),
            run=lambda q: self._run(ds, q, max_rows=1000),
        )

    def validate_sql(self, sql: str, datasource: str | None = None) -> dict[str, Any]:
        ds = self.ds(datasource)
        if self.knowledge.is_knowledge(ds):
            self.knowledge.index.sync()
        result = self._validator(ds).validate(sql, probe=True)
        result["datasource"] = ds["name"]
        result["dialect"] = self.dialect(ds)
        if result["valid"] and not result["warnings"]:
            result["next_step"] = "Run it with run_sql."
        elif result["valid"]:
            result["next_step"] = "The query runs, but read every warning: fix the SQL or explain why the warning does not apply."
        else:
            result["next_step"] = "Fix the errors and validate again."
        return result

    def run_sql(self, sql: str, question: str | None = None, max_rows: int = 100, datasource: str | None = None) -> dict[str, Any]:
        ds = self.ds(datasource)
        if self.knowledge.is_knowledge(ds):
            self.knowledge.index.sync()
        dialect = self.dialect(ds)
        try:
            guard_sources(guard_readonly(sql, dialect), self._catalog_tables(ds))
        except SqlGuardError as exc:
            raise OntologyError(str(exc)) from exc
        cap = max(1, min(int(max_rows or 100), self.settings.max_rows))
        validator = self._validator(ds)
        static = validator.validate(sql, probe=False)
        qid = new_id("q")
        try:
            result = self.connections.run_readonly(ds, sql.strip().rstrip(";"), max_rows=cap)
        except Exception as exc:  # noqa: BLE001
            err = short_error(exc)
            self.store.insert("query_log", {"id": qid, "datasource_id": ds["id"], "question": question, "sql": sql, "status": "error", "error": err, "executed_at": now()})
            raise OntologyError(f"The database returned an error: {err}. Call validate_sql for details.") from exc
        terms_used = [t["term"] for t in static["grounding"].get("terms", [])]
        self.store.insert(
            "query_log",
            {
                "id": qid, "datasource_id": ds["id"], "question": question, "sql": sql, "status": "ok",
                "row_count": len(result.rows), "truncated": result.truncated, "terms": terms_used, "executed_at": now(),
            },
        )
        warnings = list(static["errors"]) + list(static["warnings"])
        if not result.rows and static.get("valid"):
            literal_check = validator.validate(sql, probe=True)
            warnings += [w for w in literal_check["warnings"] if w not in warnings]
        out = {
            "query_id": qid,
            "datasource": ds["name"],
            "columns": result.columns,
            "rows": result.rows,
            "row_count": len(result.rows),
            "truncated": result.truncated,
            "warnings": warnings,
            "notes": static["notes"],
            "grounding": static["grounding"],
            "answer_checklist": guidance.ANSWER_CHECKLIST,
        }
        if result.truncated:
            out["notes"].append(f"Only the first {cap} rows are shown; do not present them as a complete list or total.")
        return out

    def save_example_query(self, question: str, sql: str, note: str | None = None, datasource: str | None = None) -> dict[str, Any]:
        ds = self.ds(datasource)
        check = self._validator(ds).validate(sql, probe=False)
        if not check["valid"]:
            raise OntologyError("Not saved; the SQL does not validate: " + "; ".join(check["errors"]))
        q = (question or "").strip()
        if not q:
            raise OntologyError("question is required.")
        prev = self.store.one("SELECT id FROM example_query WHERE datasource_id = ? AND lower(question) = lower(?)", (ds["id"], q))
        if prev:
            self.store.update("example_query", prev["id"], {"sql": sql.strip(), "note": note})
            eid = prev["id"]
        else:
            eid = new_id("ex")
            self.store.insert("example_query", {"id": eid, "datasource_id": ds["id"], "question": q, "sql": sql.strip(), "note": note, "created_at": now()})
        self.store.log("example_query", eid, "upsert", {"question": q})
        return {"saved": {"id": eid, "question": q}, "examples": len(self.store.examples(ds["id"]))}

    # ======================================================== export / import
    def export_ontology(self, datasource: str | None = None, fmt: str = "yaml") -> dict[str, Any]:
        ds = self.ds(datasource)
        tables = self.store.tables(ds["id"])
        doc = {
            "format": "auto-ontology-mcp/v1",
            "exported_at": now(),
            "datasource": {"name": ds["name"], "dialect": ds.get("dialect"), "url": mask_url(ds["url"]), "description": ds.get("description")},
            "tables": [
                {
                    "table": _label(t["schema_name"], t["name"]),
                    "primary_key": t.get("pk") or [],
                    "is_junction": bool(t.get("is_junction")),
                    "description": t.get("description"),
                    "columns": {c["name"]: c.get("description") for c in self.store.columns(t["id"]) if c.get("description")},
                }
                for t in tables
            ],
            "relationships": [
                {k: v for k, v in self._relationship_view(r).items() if k not in ("id", "evidence", "containment")}
                for r in self.store.relationships(ds["id"], ("accepted", "proposed", "rejected"))
            ],
            "terms": [],
            "glossary": [{"name": g["name"], "definition": g["definition"]} for g in self.store.glossary(ds["id"])],
            "example_queries": [{"question": e["question"], "sql": e["sql"], "note": e.get("note")} for e in self.store.examples(ds["id"])],
        }
        for t in self.store.terms(ds["id"]):
            doc["terms"].append(
                {
                    "name": t["name"],
                    "table": _label(t.get("schema_name"), t["table_name"]) if t.get("table_name") else None,
                    "status": t["status"],
                    "description": t.get("description"),
                    "synonyms": t.get("synonyms") or [],
                    "columns": [
                        {"column": a["column_name"], "display_name": a["display_name"], **({"description": a["description"]} if a.get("description") else {})}
                        for a in self.store.term_attributes(t["id"])
                    ],
                    "sql_attributes": [
                        {"name": s["name"], "kind": s["kind"], "status": s["status"], "description": s.get("description"), "expression": s["expression"]}
                        for s in self.store.sql_attributes(t["id"])
                    ],
                    **({"kind": t["kind"]} if (t.get("kind") or "entity") != "entity" else {}),
                    **({"body": t["body"]} if t.get("body") else {}),
                    **({"spec": t["spec"]} if t.get("spec") else {}),
                }
            )
        doc["term_relations"] = [
            {"source": r["from_name"], "relation": r["relation"], "target": r["to_name"],
             **({"description": r["description"]} if r.get("description") else {})}
            for r in self.store.all(
                """
                SELECT s.name AS from_name, r.relation, t.name AS to_name, r.description FROM term_relation r
                JOIN term s ON s.id = r.source_term_id JOIN term t ON t.id = r.target_term_id
                WHERE r.datasource_id = ? ORDER BY s.name, r.relation, t.name
                """,
                (ds["id"],),
            )
        ]
        text = yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=120) if fmt == "yaml" else json.dumps(doc, ensure_ascii=False, indent=2)
        return {"format": fmt, "document": text, "terms": len(doc["terms"]), "relationships": len(doc["relationships"])}

    def import_ontology(self, document: str, datasource: str | None = None) -> dict[str, Any]:
        ds = self.ds(datasource)
        try:
            doc = yaml.safe_load(document)
        except yaml.YAMLError as exc:
            raise OntologyError(f"Could not parse the document as YAML/JSON: {exc}") from exc
        if not isinstance(doc, dict) or doc.get("format") != "auto-ontology-mcp/v1":
            raise OntologyError("Expected a document exported by export_ontology (format: auto-ontology-mcp/v1).")
        if not self.store.tables(ds["id"]):
            raise OntologyError("Scan the datasource before importing (scan_datasource).")
        report: dict[str, list[str]] = {"terms": [], "relationships": [], "sql_attributes": [], "skipped": []}
        existing_tables = {_label(t["schema_name"], t["name"]) for t in self.store.tables(ds["id"])}
        # Knowledge entities whose tables do not exist here yet: recreate them (empty) from their spec.
        pending = [
            t for t in doc.get("terms") or []
            if t.get("spec") and self.knowledge.is_knowledge(ds) and t["spec"]["table"] not in existing_tables
        ]
        for _ in range(len(pending) + 1):
            if not pending:
                break
            retry = []
            for t in pending:
                cols = [
                    {**{k: c.get(k) for k in ("name", "display_name", "description", "required")},
                     "type": f"ref:{c['ref']}" if c["type"] == "ref" else c["type"]}
                    for c in t["spec"]["columns"]
                ]
                try:
                    self.knowledge.define_entity(t["name"], cols, t.get("description"), t["spec"]["table"], t["spec"].get("keys"), t.get("synonyms"))
                    report["terms"].append(t["name"])
                except KnowledgeError:
                    retry.append(t)
            if len(retry) == len(pending):
                report["skipped"] += [f"entity {t['name']}: its references could not be created" for t in retry]
                break
            pending = retry
        for rel in doc.get("relationships") or []:
            if self.knowledge.is_knowledge(ds) and rel.get("origin") == "declared":
                continue  # recreated with the entity tables
            try:
                self.set_relationship(
                    "add", source=rel["source"], target=rel["target"], role=rel.get("role"),
                    description=rel.get("description"), is_default=rel.get("is_default"), datasource=ds["name"],
                )
                if rel.get("status") in ("proposed", "rejected"):
                    r = self._find_rel(ds, None, rel["source"], rel["target"])
                    self.store.update("relationship", r["id"], {"status": rel["status"]})
                report["relationships"].append(f"{rel['source']} → {rel['target']}")
            except OntologyError as exc:
                report["skipped"].append(f"relationship {rel.get('source')} → {rel.get('target')}: {exc}")
        recreated = {t["name"] for t in doc.get("terms") or [] if t.get("spec") and t["name"] in report["terms"]}
        by_table: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for t in doc.get("terms") or []:
            if t.get("table") and t["name"] not in recreated and not (t.get("spec") and self.knowledge.is_knowledge(ds)):
                by_table[t["table"]].append(t)
            elif not t.get("table") and not t.get("spec"):
                try:
                    self.knowledge.save_concept(
                        t["name"], t.get("description"), body=t.get("body"), synonyms=t.get("synonyms"),
                        status=t.get("status") if t.get("status") != "draft" else None, datasource=ds["name"],
                    )
                    report["terms"].append(t["name"])
                except OntologyError as exc:
                    report["skipped"].append(f"concept {t.get('name')}: {exc}")
        for table, tlist in by_table.items():
            try:
                self.save_table_terms(
                    table,
                    [{"name": t["name"], "description": t.get("description"), "synonyms": t.get("synonyms"), "columns": t.get("columns")} for t in tlist],
                    datasource=ds["name"],
                )
                for t in tlist:
                    if t.get("status") and t["status"] != "draft":
                        self.update_term(t["name"], status=t["status"], datasource=ds["name"])
                    report["terms"].append(t["name"])
                    for s in t.get("sql_attributes") or []:
                        try:
                            self.save_sql_attribute(t["name"], s["name"], s["expression"], s.get("description"), s.get("kind", "measure"), status=s.get("status"), datasource=ds["name"])
                            report["sql_attributes"].append(f"{t['name']} › {s['name']}")
                        except OntologyError as exc:
                            report["skipped"].append(f"SQL attribute {t['name']} › {s.get('name')}: {exc}")
            except OntologyError as exc:
                report["skipped"].append(f"terms for {table}: {exc}")
        by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in doc.get("term_relations") or []:
            by_source[r["source"]].append({"relation": r["relation"], "target": r["target"], "description": r.get("description")})
        for name, rels in by_source.items():
            try:
                self.knowledge.save_concept(name, relations=rels, datasource=ds["name"])
                report.setdefault("term_relations", []).extend(f"{name} {r['relation']} {r['target']}" for r in rels)
            except OntologyError as exc:
                report["skipped"].append(f"relations of {name}: {exc}")
        if doc.get("glossary"):
            self.save_glossary(doc["glossary"], datasource=ds["name"])
        for e in doc.get("example_queries") or []:
            try:
                self.save_example_query(e["question"], e["sql"], e.get("note"), datasource=ds["name"])
            except OntologyError as exc:
                report["skipped"].append(f"example {e.get('question')!r}: {exc}")
        return {"datasource": ds["name"], "imported": {k: len(v) for k, v in report.items() if k != "skipped"}, "skipped": report["skipped"]}

    # ===================================================== ChatGPT search/fetch
    def fetch_document(self, doc_id: str) -> dict[str, Any]:
        kind, _, rid = doc_id.partition(":")
        if kind == "source":
            data = self.knowledge.get_source(rid)
            src = data["source"]
            lines = [f"# {src['title']}", "", src.get("summary") or "", "", src.get("content") or ""]
            for r in data["records"]:
                lines.append(f"- {r['records']} {r['entity']} record(s): ids {r['ids'][:20]}")
            for c in data["concepts"]:
                lines.append(f"- concept {c['name']}" + (f" ({c['locator']})" if c.get("locator") else ""))
            for p in data.get("passages") or []:
                where = ", ".join(x for x in (p.get("heading"), p.get("locator")) if x)
                lines.append(f"- passage {p['id']} ({p['kind']}{', ' + where if where else ''}): {p['text'][:300]}")
            return {"id": doc_id, "title": src["title"], "text": "\n".join(lines), "url": src.get("uri") or f"ontology://knowledge/source/{rid}", "metadata": {"kind": src.get("kind")}}
        for ds in self.store.datasources():
            if kind == "term" and self.store.one("SELECT id FROM term WHERE id = ? AND datasource_id = ?", (rid, ds["id"])):
                data = self.get_term(rid, datasource=ds["name"])
                return {"id": doc_id, "title": data["name"], "text": _markdown_term(data), "url": f"ontology://{ds['name']}/term/{rid}", "metadata": {"datasource": ds["name"], "status": data["status"]}}
            if kind == "table" and self.store.one("SELECT id FROM catalog_table WHERE id = ? AND datasource_id = ?", (rid, ds["id"])):
                tbl = self.store.table(rid)
                data = self.describe_table(_label(tbl["schema_name"], tbl["name"]), datasource=ds["name"])
                return {"id": doc_id, "title": data["table"], "text": yaml.safe_dump(data, sort_keys=False, allow_unicode=True), "url": f"ontology://{ds['name']}/table/{rid}", "metadata": {"datasource": ds["name"]}}
        raise OntologyError(f"Unknown document id {doc_id!r}.")

    def search_documents(self, query: str) -> list[dict[str, Any]]:
        results = []
        for ds in self.store.datasources():
            for hit in self._search(ds, query, limit=10):
                if hit["kind"] in ("term", "table", "source"):
                    results.append({"id": f"{hit['kind']}:{hit['id']}", "title": f"{hit['name']} ({ds['name']})", "url": f"ontology://{ds['name']}/{hit['kind']}/{hit['id']}"})
                elif hit["kind"] in ("attribute", "sql_attribute"):
                    t = self.store.one("SELECT tm.id FROM term tm WHERE tm.datasource_id = ? AND tm.name = ?", (ds["id"], hit.get("term")))
                    if t and not any(r["id"] == f"term:{t['id']}" for r in results):
                        results.append({"id": f"term:{t['id']}", "title": f"{hit['term']} ({ds['name']})", "url": f"ontology://{ds['name']}/term/{t['id']}"})
        return results[:10]


def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _sql_attr_view(s: dict[str, Any]) -> dict[str, Any]:
    v = s.get("validation") or {}
    return {
        "name": s["name"],
        "kind": s["kind"],
        "status": s["status"],
        "description": s.get("description"),
        "expression": s["expression"],
        "validated": bool(v.get("ok")),
        **({"matching_rows": v["matching_rows"]} if "matching_rows" in v else {}),
    }


def _markdown_term(data: dict[str, Any]) -> str:
    lines = [f"# {data['name']} ({data['status']})", "", data.get("description") or "", ""]
    if data.get("synonyms"):
        lines.append(f"Synonyms: {', '.join(data['synonyms'])}")
    if data.get("table"):
        lines.append(f"Table: {data.get('table')}")
    lines.append("")
    lines.append("## Attributes")
    for a in data.get("attributes") or []:
        samples = a.get("all_values") or a.get("sample_values") or []
        lines.append(f"- {a['name']}: `{a['column']}` ({a.get('type')})" + (f" e.g. {', '.join(map(str, samples[:5]))}" if samples else ""))
    if data.get("sql_attributes"):
        lines += ["", "## SQL attributes"]
        for s in data["sql_attributes"]:
            lines.append(f"- {s['name']} [{s['kind']}, {s['status']}]: `{s['expression']}` — {s.get('description') or ''}")
    if data.get("body"):
        lines += ["", data["body"]]
    if data.get("relations"):
        lines += ["", "## Relations"]
        for r in data["relations"]:
            arrow = "→" if r["direction"] == "outgoing" else "←"
            lines.append(f"- {r['relation']} {arrow} {r['term']}")
    if data.get("sources"):
        lines += ["", "## Sources"]
        for src in data["sources"]:
            lines.append(f"- {src.get('title', src.get('id'))}" + (f" ({src['locator']})" if src.get("locator") else ""))
    if data.get("related_terms"):
        lines += ["", "## Related"]
        for r in data["related_terms"]:
            lines.append(f"- {', '.join(r['terms']) or '(unmodeled table)'} via {r['join']} ({r['multiplicity']})")
    return "\n".join(lines)
