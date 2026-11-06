"""Postgres integration. Runs only when AO_TEST_PG_URL points at a disposable database, e.g.

    docker run -d --rm --name aomcp-pg -e POSTGRES_PASSWORD=pw -p 55432:5432 postgres:17
    AO_TEST_PG_URL=postgresql+psycopg://postgres:pw@127.0.0.1:55432/postgres uv run --extra postgres pytest tests/test_postgres.py
"""

from __future__ import annotations

import os
import sqlite3

import pytest

from auto_ontology_mcp.service import OntologyError, OntologyService

from conftest import DEMO_TERMS

PG_URL = os.environ.get("AO_TEST_PG_URL")
pytestmark = pytest.mark.skipif(not PG_URL, reason="set AO_TEST_PG_URL to run Postgres tests")

SCHEMA_OF = {"regions": "geo", "states": "geo", "cities": "geo"}
ORDER = ["regions", "states", "cities", "employees", "customers", "product_categories", "products", "tags", "product_tags", "orders", "order_lines"]


@pytest.fixture()
def pg_svc(settings, demo_path):
    psycopg = pytest.importorskip("psycopg")
    src = sqlite3.connect(demo_path)
    dsn = PG_URL.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(dsn, autocommit=True) as pg:
        pg.execute("DROP SCHEMA IF EXISTS geo CASCADE; DROP SCHEMA IF EXISTS sales CASCADE; CREATE SCHEMA geo; CREATE SCHEMA sales;")
        ddl_by_name = dict(src.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table'").fetchall())
        for name in ORDER:
            schema = SCHEMA_OF.get(name, "sales")
            ddl = ddl_by_name[name].replace(f"CREATE TABLE {name}", f"CREATE TABLE {schema}.{name}")
            for ref, ref_schema in (("regions", "geo"), ("product_categories", "sales"), ("orders", "sales")):
                ddl = ddl.replace(f"REFERENCES {ref}(", f"REFERENCES {ref_schema}.{ref}(")
            ddl = ddl.replace(" REAL", " NUMERIC(12,2)").replace("order_date TEXT", "order_date DATE")
            pg.execute(ddl)
            rows = src.execute(f"SELECT * FROM {name}").fetchall()
            with pg.cursor() as cur:
                cur.executemany(f"INSERT INTO {schema}.{name} VALUES ({','.join(['%s'] * len(rows[0]))})", rows)
    svc = OntologyService(settings)
    svc.add_datasource("shop", PG_URL)
    return svc


def test_postgres_scan_model_and_query(pg_svc):
    rels = {(r["source"], r["target"]): r for r in pg_svc.list_relationships(status="all")["relationships"]}
    assert rels[("sales.orders.customer_id", "sales.customers.customer_id")]["status"] == "accepted"
    assert rels[("sales.customers.delivery_city_id", "geo.cities.city_id")]["role"] == "delivery"
    assert not any(src == "geo.states.state_code" for src, _ in rels)

    for table, terms in DEMO_TERMS.items():
        pg_svc.save_table_terms(f"{SCHEMA_OF.get(table, 'sales')}.{table}", terms)
    hops = pg_svc.get_query_context(["Order Line", "State"])["join_path"]["hops"]
    assert [h["to"] for h in hops][-1] == "geo.states"

    sql = (
        "SELECT s.state_name, SUM(l.line_total) AS revenue FROM sales.order_lines l "
        "JOIN sales.orders o ON o.order_id = l.order_id JOIN sales.customers c ON c.customer_id = o.customer_id "
        "JOIN geo.cities ci ON ci.city_id = c.delivery_city_id JOIN geo.states s ON s.state_id = ci.state_id "
        "WHERE o.status = 'delivered' AND l.discount_pct > 10 GROUP BY 1"
    )
    warnings = " ".join(pg_svc.validate_sql(sql)["warnings"])
    assert "'Delivered'" in warnings and "Did you mean 0.1" in warnings
    out = pg_svc.run_sql(sql.replace("'delivered'", "'Delivered'").replace("> 10", "> 0.1"))
    assert out["row_count"] > 0


def test_postgres_is_read_only(pg_svc):
    for bad in ("DELETE FROM sales.orders", "SELECT pg_read_file('/etc/passwd')", "SELECT set_config('a', 'b', false)"):
        with pytest.raises(OntologyError):
            pg_svc.run_sql(bad)
    with pytest.raises(Exception, match="read-only"):
        pg_svc.connections.run_readonly(pg_svc.ds(), "CREATE TABLE sales.should_fail (x int)")
