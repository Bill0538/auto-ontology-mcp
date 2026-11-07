"""The three demos: structured (retail database), unstructured (documents), and both together."""

from __future__ import annotations

import pytest

from auto_ontology_mcp.demo_knowledge import CONCEPTS, remove_demo_knowledge, seed_demo_knowledge
from auto_ontology_mcp.service import OntologyService

CAUSES_OF_DECLINE = """
    WITH RECURSIVE chain(id, name, relation, depth) AS (
        SELECT id, name, NULL, 0 FROM terms WHERE name = 'Midwest Sales Decline 2024'
        UNION ALL
        SELECT r.from_term_id, r.from_term, r.relation, chain.depth + 1
        FROM term_relations r JOIN chain ON r.to_term_id = chain.id
        WHERE r.relation IN ('causes', 'part_of') AND chain.depth < 6
    )
    SELECT name, relation, depth FROM chain WHERE depth > 0 ORDER BY depth, name
"""

QUALITY_PASSAGES = """
    SELECT t.name AS issue, p.heading, snippet(passages_fts, -1, '[', ']', '…', 10) AS hit
    FROM passages_fts
    JOIN passages p ON p.id = passages_fts.passage_id
    JOIN passage_terms pt ON pt.passage_id = p.id
    JOIN terms t ON t.id = pt.term_id
    JOIN term_relations r ON r.from_term_id = t.id
    WHERE passages_fts MATCH '"returns"* OR "back"' AND r.relation = 'instance_of' AND r.to_term = 'Product Quality Issue'
    ORDER BY bm25(passages_fts)
"""

OPEN_ITEMS_WITH_REASON = """
    SELECT a.title, a.owner, a.due_on, group_concat(DISTINCT p.heading) AS sections
    FROM action_items a
    JOIN sources s ON s.id = a._source_id
    JOIN passages p ON p.source_id = s.id
    JOIN passages_fts ON passages_fts.passage_id = p.id
    WHERE a.status = 'open' AND passages_fts MATCH '"stockout"* OR "delivery"*'
    GROUP BY a.id, a.title, a.owner, a.due_on
    ORDER BY a.due_on IS NULL, a.due_on
"""

MIDWEST_MONTHS = """
    SELECT substr(o.order_date, 6, 2) AS month,
           SUM(o.order_date < '2024-01-01') AS orders_2023,
           SUM(o.order_date >= '2024-01-01') AS orders_2024
    FROM orders o
    JOIN customers c ON c.customer_id = o.customer_id
    JOIN cities ci ON ci.city_id = c.billing_city_id
    JOIN states s ON s.state_id = ci.state_id
    JOIN regions r ON r.region_id = s.region_id
    WHERE r.region_name = 'Midwest' AND o.status NOT IN ('Cancelled', 'Returned')
    GROUP BY month HAVING month IN ('01', '02', '09') ORDER BY month
"""


@pytest.fixture()
def demo(modeled: OntologyService) -> OntologyService:
    seed_demo_knowledge(modeled)
    return modeled


def test_seeding_is_repeatable_and_keeps_originals(demo):
    k = demo.knowledge
    before = demo.check_readiness()
    again = seed_demo_knowledge(demo)
    know = next(d for d in demo.check_readiness()["datasources"] if d["datasource"] == "knowledge")["knowledge"]
    assert know == next(d for d in before["datasources"] if d["datasource"] == "knowledge")["knowledge"]
    assert know["passages"] == again["passages"] == 12 and know["sources_with_original"] == 3
    note = (k.originals_dir / "Midwest sales review, Q3 2024.md").read_text()
    assert "uri: demo://docs/midwest-sales-review-2024-q3" in note and "## September stockout" in note


def test_unstructured_demo(demo):
    k = demo.knowledge
    hit = k.search_text("bestseller")["hits"][0]
    assert hit["kind"] == "term" and hit["name"] == "Bestseller Tag"
    passage = k.search_text("bestseller", kinds=["passage"])["hits"][0]
    assert passage["link"] == "[[originals/Merchandising tag guide#bestseller]]"
    assert passage["heading"] == "bestseller" and "does not mean the product sells the most units" in passage["text"]

    rows = demo.run_sql(CAUSES_OF_DECLINE, datasource="knowledge")["rows"]
    assert rows == [
        ["Fewer Repeat Orders", "part_of", 1],
        ["Delivery Delay", "causes", 2],
        ["September Stockout", "causes", 2],
        ["Fulfilment Cutover", "causes", 3],
    ]

    check = demo.validate_sql(QUALITY_PASSAGES, datasource="knowledge")
    assert check["valid"], check
    rows = demo.run_sql(QUALITY_PASSAGES, datasource="knowledge")["rows"]
    assert {r[0] for r in rows} == {"Ear Cushion Cracking", "Wobbling Chair Base"}


def test_combined_demo(demo):
    # One query over records and text in the knowledge store: open action items and the passage behind them.
    check = demo.validate_sql(OPEN_ITEMS_WITH_REASON, datasource="knowledge")
    assert check["valid"] and not check["errors"], check
    rows = demo.run_sql(OPEN_ITEMS_WITH_REASON, datasource="knowledge")["rows"]
    assert [r[:3] for r in rows] == [
        ["Move Ohio and Michigan fulfilment back to Columbus", "Min Lee", "2025-01-31"],
        ["Send a win-back offer to Midwest customers", "Dana Whitfield", None],
    ]
    assert all(set(r[3].split(",")) == {"January cutover", "September stockout"} for r in rows)

    # Across datasources: the database shows when Midwest orders fell, the documents say why.
    months = demo.run_sql(MIDWEST_MONTHS, datasource="demo")["rows"]
    assert months == [["01", 32, 17], ["02", 30, 19], ["09", 34, 16]]
    why = demo.knowledge.search_text("Midwest January September", kinds=["passage"])["hits"][0]
    assert why["heading"] == "Summary" and why["source"] == "Midwest sales review, Q3 2024"

    # The tag guide explains a finding in the sales data: bestseller products do not sell more units.
    units = demo.run_sql(
        """
        SELECT CASE WHEN EXISTS (SELECT 1 FROM product_tags pt JOIN tags t ON t.tag_id = pt.tag_id
                                 WHERE pt.product_id = p.product_id AND t.tag_name = 'bestseller') THEN 'bestseller' ELSE 'other' END AS grp,
               ROUND(AVG(u.units), 0) AS avg_units_per_product
        FROM products p JOIN (SELECT product_id, SUM(quantity) AS units FROM order_lines GROUP BY product_id) u ON u.product_id = p.product_id
        GROUP BY grp ORDER BY grp
        """,
        datasource="demo",
    )["rows"]
    assert [r[0] for r in units] == ["bestseller", "other"] and abs(units[0][1] - units[1][1]) < 100


def test_remove_demo_knowledge(demo):
    k = demo.knowledge
    k.save_passages([{"text": "My own note about habits."}], source={"title": "Journal", "kind": "note"})
    demo.export_wiki()
    assert (k.wiki_dir / "Bestseller Tag.md").exists()
    out = remove_demo_knowledge(demo)
    assert "Bestseller Tag.md" in out["removed_pages"] and not (k.wiki_dir / "Bestseller Tag.md").exists()
    assert out["removed_sources"] == 3 and out["removed_passages"] == 12 and len(out["removed_originals"]) == 3
    assert set(out["removed_concepts"]) >= {c["name"] for c in CONCEPTS}
    assert not (k.originals_dir / "Merchandising tag guide.md").exists()
    terms = {t["name"] for t in demo.list_terms(datasource="knowledge")["terms"]}
    assert "Bestseller Tag" not in terms and "Action Item" not in terms
    assert [h["source"] for h in k.search_text("habits")["hits"] if h["kind"] == "passage"] == ["Journal"]
    assert remove_demo_knowledge(demo) == {"removed_sources": 0}
