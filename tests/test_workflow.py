"""The full loop a host model runs: model, ask, validate, run, learn."""

from __future__ import annotations

import pytest

from auto_ontology_mcp.service import OntologyError


def test_modeling_progress_and_validation(svc):
    first = svc.get_modeling_worklist(limit=1)
    assert first["progress"]["remaining"] == 10
    with pytest.raises(OntologyError) as exc:
        svc.save_table_terms("orders", [{"name": "Order", "description": "x", "columns": [{"column": "no_such", "display_name": "X"}]}])
    assert "not in orders" in str(exc.value)
    saved = svc.save_table_terms(
        "orders",
        [{"name": "customer_order", "description": "A customer order.", "columns": [{"column": "order_id", "display_name": "order id"}]}],
    )
    # snake_case names are normalized, unassigned candidate columns are attached and reported.
    assert saved["saved_terms"][0]["name"] == "Customer Order"
    assert set(saved["auto_assigned_columns"]) == {"order_date", "status", "channel", "sales_rep_id"}
    assert saved["progress"]["remaining"] == 9


def test_term_name_collision_is_refused(modeled):
    with pytest.raises(OntologyError) as exc:
        modeled.save_table_terms("tags", [{"name": "Customer", "description": "x", "columns": []}])
    assert "already exists" in str(exc.value)


def test_readiness_after_modeling(modeled):
    ready = modeled.check_readiness()["datasources"][0]
    assert ready["verdict"] == "ready"
    assert ready["terms"]["total"] == 10


def test_search_handles_synonyms_case_and_korean(modeled):
    hits = modeled.search_ontology("invoice line")["hits"]
    assert hits[0]["name"] == "Order Line"
    assert modeled.search_ontology("revenue")["hits"][0]["name"] == "Order Line › Revenue"
    assert modeled.search_ontology("고객")["hits"][0]["name"] == "Customer"


def test_check_answerable_resolves_names_in_the_data(modeled):
    result = modeled.check_answerable(
        "What was revenue for the sushi roll flash drive in Pennsylvania in June 2024?",
        ["revenue", "sushi roll flash drive", "Pennsylvania"],
    )
    assert result["verdict"] == "answerable"
    by_entity = {e["entity"]: e for e in result["entities"]}
    assert by_entity["Pennsylvania"]["state"] == "covered_by_value"
    assert by_entity["sushi roll flash drive"]["value_matches"][0]["values"] == ["USB Food Flash Drive - Sushi Roll"]


def test_check_answerable_flags_missing_concepts(modeled):
    ok = modeled.check_answerable("Revenue by state in 2024", ["revenue", "state"])
    assert ok["verdict"] == "answerable"
    missing = modeled.check_answerable("Return on ad spend for flash drives", ["ad spend", "return on ad spend", "flash drive"])
    assert missing["verdict"] in ("partially_answerable", "not_answerable")
    assert any(e["entity"] == "ad spend" and e["state"] == "not_covered" for e in missing["entities"])


def test_query_context_has_join_path_and_roles(modeled):
    ctx = modeled.get_query_context(["Order Line", "State"], question="Revenue by state")
    hops = [h["on"] for h in ctx["join_path"]["hops"]]
    assert hops == [
        "order_lines.order_id = orders.order_id",
        "orders.customer_id = customers.customer_id",
        "customers.delivery_city_id = cities.city_id",
        "cities.state_id = states.state_id",
    ]
    assert any("billing" in n for n in ctx["join_path"]["role_notes"])
    assert "JOIN" in ctx["join_path"]["join_sql"]


def test_validate_catches_near_miss_literal_and_percent(modeled):
    result = modeled.validate_sql(
        "SELECT SUM(l.line_total) FROM order_lines l JOIN orders o ON o.order_id = l.order_id "
        "WHERE o.status = 'shipped' AND l.discount_pct > 20"
    )
    text = " ".join(result["warnings"])
    assert "'Shipped'" in text
    assert "Did you mean 0.2" in text
    assert result["valid"] is True
    named = modeled.validate_sql("SELECT COUNT(*) FROM products WHERE product_name = 'Sushi Roll Flash Drive'")
    assert "'USB Food Flash Drive - Sushi Roll'" in named["warnings"][0]


def test_validate_catches_unknown_column_and_bad_join(modeled):
    bad_col = modeled.validate_sql("SELECT revenue FROM order_lines")
    assert bad_col["valid"] is False
    assert "order_lines.line_total" in bad_col["errors"][0]
    bad_join = modeled.validate_sql(
        "SELECT COUNT(*) FROM orders o JOIN customers c ON o.order_id = c.customer_id"
    )
    assert bad_join["valid"] is False
    assert "orders.customer_id = customers.customer_id" in bad_join["errors"][0]
    role = modeled.validate_sql(
        "SELECT COUNT(*) FROM customers c JOIN cities ci ON ci.city_id = c.billing_city_id"
    )
    assert role["valid"] is True
    assert any("'billing' role" in n for n in role["notes"])
    assert not role["grounding"]["unmodeled_columns"]


def test_validate_catches_fan_out(modeled):
    result = modeled.validate_sql(
        "SELECT SUM(p.unit_price) FROM products p JOIN order_lines l ON l.product_id = p.product_id"
    )
    assert any(w.startswith("Fan-out") for w in result["warnings"])


@pytest.mark.parametrize(
    ("aggregate", "fans_out"),
    [
        # The one side only decides which lines count; the summed value is per line.
        ("SUM(CASE WHEN o.order_date < '2024-01-01' THEN l.line_total END)", False),
        ("SUM(IIF(o.status = 'Delivered', l.line_total, 0))", False),
        # A value computed per line from both sides (line cost) is at the line grain.
        ("SUM(l.quantity * p.unit_cost)", False),
        ("SUM(ABS(l.unit_price - p.unit_price) > 0.001)", False),
        # Summing or counting the one side across its lines double counts.
        ("SUM(p.unit_price)", True),
        ("AVG(CASE WHEN o.status = 'Delivered' THEN p.unit_cost END)", True),
        ("SUM(CASE WHEN o.status = 'Returned' THEN 1 ELSE 0 END)", True),
        ("COUNT(o.order_id)", True),
    ],
)
def test_fan_out_looks_at_what_is_aggregated(modeled, aggregate, fans_out):
    sql = (
        f"SELECT {aggregate} AS v FROM order_lines l JOIN orders o ON o.order_id = l.order_id "
        "JOIN products p ON p.product_id = l.product_id"
    )
    warnings = modeled.validate_sql(sql)["warnings"]
    assert any(w.startswith("Fan-out") for w in warnings) is fans_out, warnings


def test_run_sql_returns_rows_grounding_and_logs(modeled):
    sql = (
        "SELECT s.state_name, ROUND(SUM(l.line_total), 2) AS revenue FROM order_lines l "
        "JOIN orders o ON o.order_id = l.order_id JOIN customers c ON c.customer_id = o.customer_id "
        "JOIN cities ci ON ci.city_id = c.delivery_city_id JOIN states s ON s.state_id = ci.state_id "
        "WHERE o.status = 'Delivered' GROUP BY s.state_name ORDER BY revenue DESC"
    )
    out = modeled.run_sql(sql, question="Delivered revenue by state", max_rows=5)
    assert out["columns"] == ["state_name", "revenue"]
    assert out["row_count"] == 5 and out["truncated"] is True
    used = {t["term"] for t in out["grounding"]["terms"]}
    assert {"Order Line", "Order", "State"} <= used
    assert out["grounding"]["draft_terms"]
    assert modeled.store.scalar("SELECT COUNT(*) FROM query_log WHERE status = 'ok'") == 1


def test_empty_result_triggers_literal_check(modeled):
    out = modeled.run_sql("SELECT COUNT(*) FROM orders WHERE channel = 'online'")
    assert out["rows"] == [[0]]
    out = modeled.run_sql("SELECT order_id FROM orders WHERE channel = 'web'")
    assert out["rows"] == []
    assert any("'Web'" in w for w in out["warnings"])


def test_lookup_values(modeled):
    found = modeled.lookup_values("products.product_name", contains="sushi")
    assert found["matches"][0]["value"] == "USB Food Flash Drive - Sushi Roll"
    top = modeled.lookup_values("orders.channel")
    assert top["top_values"][0]["value"] == "Web"


def test_sql_attributes_validate_and_certify(modeled):
    saved = modeled.save_sql_attribute("Order", "Delivered Orders", "orders.status = 'Delivered'", "Orders that reached the customer.", kind="filter")
    assert saved["sql_attribute"]["validated"] and saved["sql_attribute"]["matching_rows"] > 0
    with pytest.raises(OntologyError) as exc:
        modeled.save_sql_attribute("Order Line", "Broken", "SUM(order_lines.no_such_col)", kind="measure")
    assert "rejected the expression" in str(exc.value)
    modeled.save_sql_attribute("Order Line", "Net Revenue", "SUM(order_lines.line_total)", "Revenue after discounts.", status="certified")
    changed = modeled.save_sql_attribute("Order Line", "Net Revenue", "SUM(order_lines.line_total * 1.0)")
    assert changed["sql_attribute"]["status"] == "draft"


def test_search_and_answerability_after_saving_sql_attributes(modeled):
    modeled.save_sql_attribute("Order", "Delivered Orders", "orders.status = 'Delivered'", "Orders that reached the customer.", kind="filter")
    hits = modeled.search_ontology("delivered orders")["hits"]
    assert hits[0]["kind"] == "sql_attribute" and hits[0]["sql_kind"] == "filter"
    assert modeled.check_answerable("How many delivered orders?", ["delivered orders"])["verdict"] == "answerable"


def test_suggestions_from_history_bridge_and_profile(modeled):
    modeled.run_sql("SELECT COUNT(*) FROM orders o WHERE o.channel = 'Store'", question="store orders")
    sugg = modeled.suggest_sql_attributes(limit=50)["candidates"]
    exprs = [c["expression"] for c in sugg]
    assert any("o.channel = 'Store'" in e for e in exprs)
    assert any("product_tags" in e and "JOIN" in e for e in exprs)
    assert any(e.startswith("SUM(order_lines.line_total)") for e in exprs)


def test_certification_and_reset(modeled):
    modeled.update_term("Customer", status="certified")
    assert modeled.get_term("Customer")["status"] == "certified"
    modeled.update_term("Customer", description="Changed meaning.")
    assert modeled.get_term("Customer")["status"] == "draft"
    modeled.update_term("Customer", add_synonyms=["Client"])
    assert modeled.get_term("client")["name"] == "Customer"


def test_examples_feed_context(modeled):
    sql = "SELECT COUNT(*) FROM orders WHERE status = 'Cancelled'"
    modeled.save_example_query("How many orders were cancelled?", sql, note="Cancelled status only")
    ctx = modeled.get_query_context(["Order"], question="number of cancelled orders")
    assert ctx["verified_examples"][0]["sql"] == sql


def test_glossary(modeled):
    modeled.save_glossary([{"name": "AOV", "definition": "average order value"}])
    assert modeled.search_ontology("AOV")["hits"][0]["kind"] == "glossary"


def test_export_import_roundtrip(modeled, settings, demo_path, tmp_path):
    from dataclasses import replace

    from auto_ontology_mcp.service import OntologyService

    modeled.save_sql_attribute("Order", "Delivered Orders", "orders.status = 'Delivered'", kind="filter")
    modeled.update_term("Customer", status="certified")
    doc = modeled.export_ontology(fmt="yaml")["document"]

    fresh = OntologyService(replace(settings, store_path=tmp_path / "other.db"))
    fresh.add_datasource("demo", f"sqlite:///{demo_path}")
    report = fresh.import_ontology(doc)
    assert report["imported"]["terms"] == 10
    assert not report["skipped"]
    assert fresh.get_term("Customer")["status"] == "certified"
    assert fresh.get_term("Order")["sql_attributes"][0]["name"] == "Delivered Orders"
    rels = {(r["source"], r["target"]): r for r in fresh.list_relationships(status="all")["relationships"]}
    assert rels[("orders.sales_rep_id", "employees.employee_id")]["status"] == "accepted"


def test_chatgpt_search_and_fetch(modeled):
    results = modeled.search_documents("customer")
    assert results and results[0]["id"].startswith("term:")
    doc = modeled.fetch_document(results[0]["id"])
    assert doc["text"].startswith("# Customer")
