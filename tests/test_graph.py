"""Join paths: a path through a shared parent is not a link between two rows."""

from __future__ import annotations

from auto_ontology_mcp.graph import JoinGraph, shared_parents

TABLES = [{"id": name, "name": name, "schema_name": None} for name in ("payments", "invoices", "plans", "accounts")]


def _rel(source: str, column: str, target: str, cardinality: str = "many_to_one") -> dict:
    return {
        "id": f"{source}.{column}",
        "status": "accepted",
        "source_table_id": source,
        "target_table_id": target,
        "source_column": column,
        "target_column": "id",
        "cardinality": cardinality,
        "is_default": True,
        "role": None,
    }


def _graph() -> JoinGraph:
    # A payment pays an invoice, the invoice bills a plan, and payments and plans both belong to an
    # account. Each account has had one plan so far, so the plan → account join was inferred as
    # one-to-one from the data.
    return JoinGraph(
        [
            _rel("payments", "account_id", "accounts"),
            _rel("payments", "invoice_id", "invoices"),
            _rel("invoices", "plan_id", "plans"),
            _rel("plans", "account_id", "accounts", cardinality="one_to_one"),
        ],
        TABLES,
    )


def test_shortest_path_through_a_shared_parent_is_flagged_by_key_direction():
    hops = _graph().path("payments", "plans")
    assert [h["on"] for h in hops] == ["payments.account_id = accounts.id", "accounts.id = plans.account_id"]
    # The second hop is one-to-one by inferred cardinality, yet both hops reference accounts.
    assert [h["referenced"] for h in hops] == ["accounts", "accounts"]
    assert shared_parents(hops) == [{"parent": "accounts", "children": ["payments", "plans"]}]


def test_linked_path_climbs_through_the_rows_instead():
    g = _graph()
    hops = g.linked_path("payments", "plans")
    assert [h["on"] for h in hops] == ["payments.invoice_id = invoices.id", "invoices.plan_id = plans.id"]
    assert shared_parents(hops) == []
    assert g.best_path("payments", "plans") == hops
    # get_query_context's tree takes the linked path too.
    tree, unreachable = g.connect(["payments", "plans"])
    assert tree == hops and unreachable == []


def test_linked_path_is_none_when_only_a_shared_parent_connects():
    g = JoinGraph([_rel("payments", "account_id", "accounts"), _rel("plans", "account_id", "accounts")], TABLES)
    assert g.linked_path("payments", "plans") is None
    assert shared_parents(g.best_path("payments", "plans"))[0]["parent"] == "accounts"
    # Going down and back up through a table that references both is a link (a bridge).
    assert [h["to"] for h in g.linked_path("accounts", "payments")] == ["payments"]
