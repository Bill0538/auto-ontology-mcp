from __future__ import annotations


def _rels(svc, status="all"):
    out = svc.list_relationships(status=status)["relationships"]
    return {(r["source"], r["target"]): r for r in out}


def test_scan_reads_tables_and_profiles(svc):
    ready = svc.check_readiness()["datasources"][0]
    assert ready["can_execute_sql"] is True
    assert ready["tables"] == 11
    assert ready["verdict"] == "not_modeled"

    table = svc.describe_table("orders")
    cols = {c["column"]: c for c in table["columns"]}
    assert table["row_count"] == 3200
    assert cols["order_date"]["format"] == "YYYY-MM-DD"
    # Small text enums get their complete value set, rare values included.
    assert set(cols["status"]["all_values"]) == {"Delivered", "Shipped", "Placed", "Cancelled", "Returned"}


def test_declared_and_inferred_foreign_keys(svc):
    rels = _rels(svc)
    assert rels[("order_lines.order_id", "orders.order_id")]["origin"] == "declared"
    # Undeclared keys are found by name and confirmed by value containment.
    inferred = rels[("orders.customer_id", "customers.customer_id")]
    assert inferred["origin"] == "inferred" and inferred["status"] == "accepted"
    assert inferred["containment"] == 1.0
    assert rels[("cities.state_id", "states.state_id")]["status"] == "accepted"
    # Two roles towards the same table, each with its role.
    assert rels[("customers.delivery_city_id", "cities.city_id")]["role"] == "delivery"
    assert rels[("customers.billing_city_id", "cities.city_id")]["role"] == "billing"
    # A self reference is proposed for review, not auto-accepted.
    assert rels[("employees.manager_id", "employees.employee_id")]["status"] == "proposed"


def test_unlinked_identifier_columns_are_reported(svc):
    unlinked = {u["column"]: u for u in svc.list_relationships()["unlinked_id_columns"]}
    assert "orders.sales_rep_id" in unlinked
    assert "states.state_code" not in unlinked  # the table's own code, not a reference
    assert "employees.employee_id" in unlinked["orders.sales_rep_id"]["value_compatible_targets"]


def test_bridge_table_detected_and_excluded_from_worklist(svc):
    assert svc.describe_table("product_tags")["junction"].startswith("every non-key column")
    worklist = svc.get_modeling_worklist(limit=10)
    assert "product_tags" in worklist["tables_with_only_foreign_keys"]
    assert "product_tags" not in [t["table"] for t in worklist["tables"]]


def test_rescan_keeps_reviewed_relationships(svc):
    svc.set_relationship("reject", source="employees.manager_id", target="employees.employee_id")
    svc.set_relationship("add", source="orders.sales_rep_id", target="employees.employee_id", role="sales rep")
    svc.scan_datasource()
    rels = _rels(svc)
    assert rels[("employees.manager_id", "employees.employee_id")]["status"] == "rejected"
    assert rels[("orders.sales_rep_id", "employees.employee_id")]["origin"] == "manual"


def test_default_role_can_be_changed(svc):
    svc.set_relationship("update", source="customers.delivery_city_id", target="cities.city_id", is_default=True)
    svc.scan_datasource()
    rels = _rels(svc)
    assert rels[("customers.delivery_city_id", "cities.city_id")]["is_default"] is True
    assert rels[("customers.billing_city_id", "cities.city_id")]["is_default"] is False
