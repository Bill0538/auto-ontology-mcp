"""Catalog extraction: read schemas, profile columns, infer keys and joins.

This is Auto Ontology's ingestion step ("read sources: extract schemas,
profile columns, and sample values into the catalog") together with the
deterministic half of relationship resolution. Nothing here calls a model.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

import sqlalchemy as sa

from . import inference
from .datasources import Connections, qualified_name, quote_ident, short_error, sqlglot_dialect
from .profiling import LOW_CARDINALITY_MAX, profile_rows
from .sqlcheck import transpile
from .store import Store, new_id, now

logger = logging.getLogger(__name__)

_SYSTEM_SCHEMAS = {
    "information_schema", "pg_catalog", "pg_toast", "sys", "guest", "mysql",
    "performance_schema", "crdb_internal", "pg_extension",
}


def _default_schemas(inspector: sa.Inspector, backend: str) -> list[str | None]:
    if backend in ("sqlite", "mysql", "mariadb"):
        return [None]
    try:
        names = inspector.get_schema_names()
    except Exception:
        return [None]
    keep = [
        n for n in names
        if n.lower() not in _SYSTEM_SCHEMAS and not n.lower().startswith(("pg_temp", "pg_toast", "db_"))
    ]
    return keep or [None]


class Scanner:
    def __init__(self, store: Store, connections: Connections, sample_rows: int) -> None:
        self.store = store
        self.connections = connections
        self.sample_rows = sample_rows

    # ------------------------------------------------------------------ helpers
    def _run(self, ds: dict[str, Any], sql: str, max_rows: int | None = None) -> list[list[Any]]:
        dialect = sqlglot_dialect(ds.get("dialect"))
        return self.connections.run_readonly(ds, transpile(sql, dialect), max_rows=max_rows).rows

    @staticmethod
    def _qualify(table: dict[str, Any]) -> str:
        return qualified_name(table.get("schema_name") or None, table["name"])

    # --------------------------------------------------------------------- scan
    def scan(
        self,
        ds: dict[str, Any],
        *,
        schemas: list[str] | None = None,
        profile: bool = True,
        infer: bool = True,
    ) -> dict[str, Any]:
        engine = self.connections.engine(ds)
        backend = engine.dialect.name
        inspector = sa.inspect(engine)
        wanted = schemas or ds.get("schemas") or None
        schema_list: list[str | None] = list(wanted) if wanted else _default_schemas(inspector, backend)
        warnings: list[str] = []

        extracted: list[dict[str, Any]] = []
        for schema in schema_list:
            try:
                names = [(n, "table") for n in inspector.get_table_names(schema=schema)]
                names += [(n, "view") for n in inspector.get_view_names(schema=schema)]
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"Could not list tables in schema {schema!r}: {short_error(exc)}")
                continue
            for name, kind in names:
                if backend == "sqlite" and name.startswith("sqlite_"):
                    continue
                try:
                    cols = inspector.get_columns(name, schema=schema)
                    pk = (inspector.get_pk_constraint(name, schema=schema) or {}).get("constrained_columns") or []
                    fks = inspector.get_foreign_keys(name, schema=schema) if kind == "table" else []
                except Exception as exc:  # noqa: BLE001
                    warnings.append(f"Could not read {schema or ''}.{name}: {short_error(exc)}")
                    continue
                try:
                    comment = (inspector.get_table_comment(name, schema=schema) or {}).get("text")
                except Exception:
                    comment = None
                extracted.append(
                    {"schema": schema or "", "name": name, "kind": kind, "columns": cols, "pk": pk, "fks": fks, "comment": comment}
                )

        table_ids = self._upsert_catalog(ds, extracted)
        self._sync_declared_fks(ds, extracted, table_ids, warnings)

        profiled = 0
        if profile:
            for table in self.store.tables(ds["id"]):
                if self._profile_table(ds, table, warnings):
                    profiled += 1

        inferred_summary: dict[str, Any] = {}
        if infer:
            inferred_summary = self.infer_relationships(ds, probe=profile)

        with self.store.tx() as con:
            con.execute(
                "UPDATE datasource SET scanned_at = ?, dialect = ? WHERE id = ?",
                (now(), backend, ds["id"]),
            )
        self.store.log("datasource", ds["id"], "scan", {"tables": len(extracted), "warnings": warnings[:20]})
        return {
            "tables": len(extracted),
            "columns": sum(len(t["columns"]) for t in extracted),
            "profiled_tables": profiled,
            **inferred_summary,
            "warnings": warnings[:30],
        }

    def _upsert_catalog(self, ds: dict[str, Any], extracted: list[dict[str, Any]]) -> dict[tuple[str, str], str]:
        existing = {(t["schema_name"], t["name"]): t for t in self.store.tables(ds["id"])}
        seen: dict[tuple[str, str], str] = {}
        with self.store.tx() as con:
            for t in extracted:
                key = (t["schema"], t["name"])
                row = existing.get(key)
                pk = list(t["pk"])
                if row is None:
                    tid = new_id("tbl")
                    self.store.insert(
                        "catalog_table",
                        {
                            "id": tid, "datasource_id": ds["id"], "schema_name": t["schema"], "name": t["name"],
                            "table_type": t["kind"], "description": t["comment"], "pk": pk, "pk_inferred": 0,
                        },
                        con,
                    )
                else:
                    tid = row["id"]
                    values: dict[str, Any] = {"table_type": t["kind"]}
                    if t["comment"]:
                        values["description"] = t["comment"]
                    if pk or not row.get("pk_inferred"):
                        values["pk"] = pk
                        values["pk_inferred"] = 0
                    self.store.update("catalog_table", tid, values, con)
                seen[key] = tid

                old_cols = {c["name"]: c for c in (con.execute("SELECT id, name, description FROM catalog_column WHERE table_id = ?", (tid,)).fetchall())}
                new_names = set()
                for ordinal, col in enumerate(t["columns"]):
                    name = col["name"]
                    new_names.add(name)
                    try:
                        dtype = str(col["type"])
                    except Exception:
                        dtype = type(col["type"]).__name__
                    if name in old_cols:
                        values = {"data_type": dtype, "is_nullable": bool(col.get("nullable", True)), "ordinal": ordinal}
                        if col.get("comment"):
                            values["description"] = col["comment"]
                        self.store.update("catalog_column", old_cols[name]["id"], values, con)
                    else:
                        self.store.insert(
                            "catalog_column",
                            {
                                "id": new_id("col"), "table_id": tid, "name": name, "data_type": dtype,
                                "is_nullable": bool(col.get("nullable", True)), "ordinal": ordinal,
                                "description": col.get("comment"),
                            },
                            con,
                        )
                for name, old in old_cols.items():
                    if name not in new_names:
                        con.execute("DELETE FROM catalog_column WHERE id = ?", (old["id"],))
            for key, row in existing.items():
                if key not in seen:
                    con.execute("DELETE FROM catalog_table WHERE id = ?", (row["id"],))
        return seen

    def _column_index(self, ds: dict[str, Any]) -> dict[tuple[str, str, str], dict[str, Any]]:
        return {
            ((c["schema_name"] or ""), c["table_name"], c["name"]): c
            for c in self.store.columns_for_datasource(ds["id"])
        }

    def _sync_declared_fks(
        self, ds: dict[str, Any], extracted: list[dict[str, Any]], table_ids: dict[tuple[str, str], str], warnings: list[str]
    ) -> None:
        index = self._column_index(ds)
        with self.store.tx() as con:
            for t in extracted:
                for fk in t["fks"]:
                    target_schema = fk.get("referred_schema") or t["schema"]
                    if ds.get("dialect") == "sqlite" or not t["schema"]:
                        target_schema = fk.get("referred_schema") or ""
                    pairs = zip(fk.get("constrained_columns") or [], fk.get("referred_columns") or [])
                    for src, dst in pairs:
                        s = index.get((t["schema"], t["name"], src))
                        d = index.get((target_schema, fk.get("referred_table"), dst))
                        if not s or not d:
                            warnings.append(
                                f"Declared FK {t['name']}.{src} → {fk.get('referred_table')}.{dst} points outside the scanned tables"
                            )
                            continue
                        row = con.execute(
                            "SELECT id FROM relationship WHERE source_column_id = ? AND target_column_id = ?",
                            (s["id"], d["id"]),
                        ).fetchone()
                        if row:
                            con.execute(
                                "UPDATE relationship SET origin = 'declared', status = 'accepted', confidence = 1.0, updated_at = ? WHERE id = ?",
                                (now(), row["id"]),
                            )
                        else:
                            self.store.insert(
                                "relationship",
                                {
                                    "id": new_id("rel"), "datasource_id": ds["id"], "source_column_id": s["id"],
                                    "target_column_id": d["id"], "origin": "declared", "status": "accepted",
                                    "confidence": 1.0, "evidence": {"reasons": ["declared foreign key constraint"]},
                                    "is_default": 1, "updated_at": now(),
                                },
                                con,
                            )

    def _profile_table(self, ds: dict[str, Any], table: dict[str, Any], warnings: list[str]) -> bool:
        columns = self.store.columns(table["id"])
        if not columns:
            return False
        qualified = self._qualify(table)
        dialect = sqlglot_dialect(ds.get("dialect"))
        try:
            result = self.connections.run_readonly(
                ds, transpile(f"SELECT * FROM {qualified} LIMIT {self.sample_rows}", dialect), max_rows=self.sample_rows
            )
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"Profiling {table['name']} failed: {short_error(exc)}")
            return False
        stats = profile_rows(columns, result.columns, result.rows)
        row_count = None
        try:
            row_count = self._run(ds, f"SELECT COUNT(*) FROM {qualified}")[0][0]
        except Exception:  # noqa: BLE001
            pass

        by_name = {c["name"]: c for c in columns}
        with self.store.tx() as con:
            for name, s in stats.items():
                col = by_name.get(name)
                if not col:
                    continue
                samples = s["sample_values"]
                complete = False
                if s["needs_distinct_probe"]:
                    try:
                        q = quote_ident(name)
                        rows = self._run(
                            ds,
                            f"SELECT DISTINCT {q} FROM {qualified} WHERE {q} IS NOT NULL LIMIT {LOW_CARDINALITY_MAX + 1}",
                        )
                        values = [r[0] for r in rows]
                        if len(values) <= LOW_CARDINALITY_MAX:
                            merged = list(samples)
                            for v in values:
                                if v not in merged and not (isinstance(v, str) and len(v) > 60):
                                    merged.append(v)
                            samples = merged
                            complete = True
                    except Exception:  # noqa: BLE001
                        pass
                self.store.update(
                    "catalog_column",
                    col["id"],
                    {
                        "sample_values": samples,
                        "is_unique": s["is_unique"],
                        "null_fraction": s["null_fraction"],
                        "distinct_in_sample": s["distinct_in_sample"],
                        "min_value": s["min_value"],
                        "max_value": s["max_value"],
                        "format": s["format"],
                        "value_set_complete": complete,
                    },
                    con,
                )
            self.store.update("catalog_table", table["id"], {"row_count": row_count, "profiled_at": now()}, con)
        return True

    # --------------------------------------------------------------- inference
    def infer_relationships(self, ds: dict[str, Any], *, probe: bool = True) -> dict[str, Any]:
        tables = self.store.tables(ds["id"])
        columns_by_table = {t["id"]: self.store.columns(t["id"]) for t in tables}

        # 1. Primary keys the schema does not declare.
        inferred_pks = []
        with self.store.tx() as con:
            for t in tables:
                if t.get("pk") and not t.get("pk_inferred"):
                    continue
                pk = inference.infer_primary_key(t, columns_by_table[t["id"]])
                if pk:
                    t["pk"] = pk
                    self.store.update("catalog_table", t["id"], {"pk": pk, "pk_inferred": 1}, con)
                    inferred_pks.append(f"{t['name']}.{pk[0]}")

        # 2. FK candidates by name, confirmed by value containment.
        existing = {
            (r["source_column_id"], r["target_column_id"]): r
            for r in self.store.relationships(ds["id"], ("accepted", "proposed", "rejected"))
        }
        locked = {k for k, r in existing.items() if r["origin"] in ("declared", "manual") or (r.get("evidence") or {}).get("reviewed")}
        candidates, unlinked = inference.candidate_pairs(tables, columns_by_table, locked)
        kept: set[tuple[str, str]] = set()
        counts = {"accepted": 0, "proposed": 0}
        with_probe = probe
        for cand in candidates:
            key = (cand.source["id"], cand.target["id"])
            if key in locked:
                kept.add(key)
                continue
            if with_probe:
                inference.probe_containment(
                    cand,
                    run=lambda sql: self._run(ds, sql),
                    qualify=self._qualify,
                    quote=quote_ident,
                )
            verdict = inference.decide(cand)
            if verdict is None:
                continue
            status, confidence = verdict
            counts[status] += 1
            kept.add(key)
            evidence = {
                "reasons": cand.reasons,
                "name_score": cand.name_score,
                "containment": cand.containment,
                "probe_size": cand.probe_size,
            }
            with self.store.tx() as con:
                row = existing.get(key)
                values = {
                    "origin": "inferred", "status": status, "confidence": confidence, "evidence": evidence,
                    "role": cand.role, "updated_at": now(),
                }
                if row:
                    self.store.update("relationship", row["id"], values, con)
                else:
                    self.store.insert(
                        "relationship",
                        {"id": new_id("rel"), "datasource_id": ds["id"], "source_column_id": key[0], "target_column_id": key[1], "is_default": 1, **values},
                        con,
                    )
        # Inferred rows no longer supported by evidence disappear (reviewed ones stay).
        with self.store.tx() as con:
            for key, row in existing.items():
                if row["origin"] == "inferred" and key not in kept and key not in locked:
                    con.execute("DELETE FROM relationship WHERE id = ?", (row["id"],))

        self._refresh_cardinality_and_defaults(ds)
        junctions = self._refresh_junctions(ds, tables, columns_by_table)
        return {
            "inferred_primary_keys": inferred_pks,
            "relationships_auto_accepted": counts["accepted"],
            "relationships_proposed": counts["proposed"],
            "junction_tables": junctions,
            "unlinked_id_columns": [f"{u['table']['name']}.{u['column']['name']}" for u in unlinked][:50],
        }

    def _refresh_cardinality_and_defaults(self, ds: dict[str, Any]) -> None:
        rels = self.store.relationships(ds["id"], ("accepted", "proposed"))
        groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        with self.store.tx() as con:
            for r in rels:
                card = "one_to_one" if r.get("source_is_unique") else "many_to_one"
                con.execute("UPDATE relationship SET cardinality = ? WHERE id = ?", (card, r["id"]))
                if r["status"] == "accepted":
                    groups[(r["source_table_id"], r["target_table_id"])].append(r)
            for group in groups.values():
                if len(group) == 1:
                    if not (group[0].get("evidence") or {}).get("reviewed"):
                        con.execute("UPDATE relationship SET is_default = 1 WHERE id = ?", (group[0]["id"],))
                    continue
                if any((r.get("evidence") or {}).get("default_reviewed") for r in group):
                    continue
                chosen = inference.default_join_choice(group)
                for r in group:
                    con.execute("UPDATE relationship SET is_default = ? WHERE id = ?", (1 if r["id"] == chosen else 0, r["id"]))

    def _refresh_junctions(
        self, ds: dict[str, Any], tables: list[dict[str, Any]], columns_by_table: dict[str, list[dict[str, Any]]]
    ) -> list[str]:
        fk_cols: dict[str, set[str]] = defaultdict(set)
        for r in self.store.relationships(ds["id"], ("accepted",)):
            fk_cols[r["source_table_id"]].add(r["source_column"])
        found = []
        with self.store.tx() as con:
            for t in tables:
                if (t.get("junction_reason") or "").startswith("host:"):
                    if t.get("is_junction"):
                        found.append(t["name"])
                    continue
                verdict = inference.junction_verdict(t, columns_by_table[t["id"]], fk_cols.get(t["id"], set()))
                if verdict is None:
                    self.store.update("catalog_table", t["id"], {"is_junction": 0, "junction_reason": None}, con)
                    continue
                certain, reason = verdict
                self.store.update(
                    "catalog_table", t["id"],
                    {"is_junction": 1 if certain else 0, "junction_reason": reason if certain else f"candidate: {reason}"},
                    con,
                )
                found.append(t["name"] if certain else f"{t['name']} (candidate)")
        return found
