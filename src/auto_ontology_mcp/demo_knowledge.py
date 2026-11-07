"""Demo documents for the knowledge store, about the same fictional store as the demo database.

The retail database (``demo``) says what happened: orders, revenue, returns. These
three documents say why: a meeting note on the Midwest slump, the merchandising
team's tag guide and the support team's quality digest. They carry no numbers the
database can compute, so a combined answer takes its figures from SQL and its
explanations, with citations, from the documents.

``seed_demo_knowledge`` stores them the way a host model would after reading
them: each document as a source whose full text is kept as an original in the
Obsidian vault, its passages, the concepts and causal links it describes, and
the action items it decides as records. Everything it adds comes from
``demo://`` sources, which is how ``remove_demo_knowledge`` finds it again.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .vault import SAVED_BY

if TYPE_CHECKING:  # pragma: no cover
    from .service import OntologyService

DEMO_URI = "demo://"
NOTICE = "Fictional demo document for auto-ontology-mcp; it describes the synthetic demo database."

MIDWEST_REVIEW = f"""# Midwest sales review, Q3 2024

{NOTICE}

Attendees: Dana Whitfield (VP Sales), Min Lee (Sales Manager), the fulfilment team.

## Summary
Midwest orders fell behind last year twice in 2024: in January and February, and again in September. Customers in Ohio and Michigan were hit hardest; Illinois less so.

## January cutover
On January 8, 2024 Midwest fulfilment moved from our Columbus warehouse to Lakeshore Logistics, a third-party distributor. Labels and stock counts did not carry over cleanly, and for about six weeks delivery took eight days instead of three. Several repeat customers told support they ordered elsewhere until deliveries recovered.

## September stockout
Lakeshore ran out of Standing Desk and Office Chair stock in the first two weeks of September after a missed reorder. Orders that included furniture were held until stock arrived, and many regular customers did not place their usual orders that month.

## Decisions
- Move Ohio and Michigan fulfilment back to Columbus by January 31, 2025. Owner: Min Lee.
- Send a win-back offer to Midwest customers who have not ordered since August. Owner: Dana Whitfield.
- Start a weekly furniture stock report from Lakeshore. Owner: Min Lee. Done in October.
"""

TAG_GUIDE = f"""# Merchandising tag guide

{NOTICE}

## How tags are assigned
Buyers assign tags by hand when a product is listed and at each quarterly review. Tags decide placement on the website and in stores; they are not calculated from sales.

## bestseller
A product the buyers want to feature on the home page. It is a placement decision and does not mean the product sells the most units.

## clearance
Stock we want to sell down before a product is replaced or discontinued. The tag does not change the price by itself: discounts come from campaigns and apply to tagged and untagged products alike.

## Other tags
- premium: higher-end materials or build.
- new arrival: listed in the last 90 days.
- eco-friendly: recycled materials or a take-back program.
- gift idea: featured in the holiday gift guides.
"""

QUALITY_DIGEST = f"""# Product quality digest, 2024

{NOTICE}

## Noise Cancelling Headphones
The most common complaint was ear cushions that cracked after a few months, and it was the product customers sent back most often. A new cushion supplier starts in spring 2025.

## Office Chair
Customers reported a wobbling base on chairs from one production batch. Several chairs came back before support started sending replacement bases in June.

## Portable SSD 1TB
Heavy users reported the drive slowing down when it got hot. The supplier ended production, so the drive is discontinued from January 2025 and remaining stock is sold through December 2024.

## Camping Stove
The supplier stopped making the stove's gas valve. We are selling the remaining stock and will not reorder; the stove is discontinued from January 2025.
"""

SOURCES: dict[str, dict[str, Any]] = {
    "midwest": {
        "title": "Midwest sales review, Q3 2024", "kind": "meeting notes", "author": "Min Lee", "published_on": "2024-10-08",
        "uri": f"{DEMO_URI}docs/midwest-sales-review-2024-q3", "summary": "Why Midwest orders fell in 2024 and what was decided.",
        "original_text": MIDWEST_REVIEW,
    },
    "tags": {
        "title": "Merchandising tag guide", "kind": "wiki", "author": "Merchandising team", "published_on": "2023-11-20",
        "uri": f"{DEMO_URI}docs/merchandising-tag-guide", "summary": "What each product tag means and who assigns it.",
        "original_text": TAG_GUIDE,
    },
    "quality": {
        "title": "Product quality digest, 2024", "kind": "report", "author": "Customer support", "published_on": "2024-12-15",
        "uri": f"{DEMO_URI}docs/product-quality-digest-2024", "summary": "Product problems customers reported in 2024 and what changes.",
        "original_text": QUALITY_DIGEST,
    },
}

CONCEPTS: list[dict[str, Any]] = [
    {"name": "Midwest Sales Decline 2024", "source": "midwest", "locator": "Summary",
     "description": "Midwest orders fell behind 2023 in January-February and September 2024, most in Ohio and Michigan.",
     "evidence": "Midwest orders fell behind last year twice in 2024"},
    {"name": "Fulfilment Cutover", "source": "midwest", "locator": "January cutover", "synonyms": ["Lakeshore cutover"],
     "description": "The January 8, 2024 move of Midwest fulfilment from the Columbus warehouse to Lakeshore Logistics.",
     "relations": [{"relation": "causes", "target": "Delivery Delay", "evidence": "for about six weeks delivery took eight days instead of three"}]},
    {"name": "Delivery Delay", "source": "midwest", "locator": "January cutover",
     "description": "Midwest deliveries taking about eight days instead of three after the cutover.",
     "relations": [{"relation": "causes", "target": "Fewer Repeat Orders", "evidence": "Several repeat customers told support they ordered elsewhere"}]},
    {"name": "September Stockout", "source": "midwest", "locator": "September stockout",
     "description": "Lakeshore ran out of Standing Desk and Office Chair stock in early September 2024 after a missed reorder.",
     "relations": [{"relation": "causes", "target": "Fewer Repeat Orders", "evidence": "many regular customers did not place their usual orders that month"}]},
    {"name": "Fewer Repeat Orders", "source": "midwest", "locator": "Summary",
     "description": "Regular Midwest customers ordering less often while deliveries or stock were unreliable.",
     "relations": [{"relation": "part_of", "target": "Midwest Sales Decline 2024"}]},
    {"name": "Lakeshore Logistics", "source": "midwest", "locator": "January cutover",
     "description": "Third-party distributor that has handled Midwest fulfilment since January 2024.",
     "relations": [{"relation": "related_to", "target": "Fulfilment Cutover"}]},
    {"name": "Merchandising Tag", "source": "tags", "locator": "How tags are assigned",
     "description": "A label buyers assign by hand to decide where a product is placed; not calculated from sales."},
    {"name": "Bestseller Tag", "source": "tags", "locator": "bestseller", "synonyms": ["bestseller"],
     "description": "A home-page placement chosen by buyers; it does not mean the product sells the most units.",
     "relations": [{"relation": "is_a", "target": "Merchandising Tag"}]},
    {"name": "Clearance Tag", "source": "tags", "locator": "clearance", "synonyms": ["clearance"],
     "description": "Stock to sell down before a product is replaced; the tag does not change the price by itself.",
     "relations": [{"relation": "is_a", "target": "Merchandising Tag"}]},
    {"name": "Product Quality Issue", "source": "quality", "locator": "Product quality digest, 2024",
     "description": "A problem customers reported with a product, from the support team's digest."},
    {"name": "Ear Cushion Cracking", "source": "quality", "locator": "Noise Cancelling Headphones",
     "description": "Noise Cancelling Headphones ear cushions cracking after a few months.",
     "relations": [{"relation": "instance_of", "target": "Product Quality Issue"},
                   {"relation": "causes", "target": "Product Returns", "evidence": "it was the product customers sent back most often"}]},
    {"name": "Wobbling Chair Base", "source": "quality", "locator": "Office Chair",
     "description": "Office Chair bases from one production batch wobbling.",
     "relations": [{"relation": "instance_of", "target": "Product Quality Issue"},
                   {"relation": "causes", "target": "Product Returns", "evidence": "Several chairs came back"}]},
    {"name": "SSD Heat Slowdown", "source": "quality", "locator": "Portable SSD 1TB",
     "description": "Portable SSD 1TB slowing down when hot; the supplier ended production.",
     "relations": [{"relation": "instance_of", "target": "Product Quality Issue"},
                   {"relation": "causes", "target": "Product Discontinuation"}]},
    {"name": "Stove Valve Shortage", "source": "quality", "locator": "Camping Stove",
     "description": "The Camping Stove's gas valve is no longer made by the supplier.",
     "relations": [{"relation": "causes", "target": "Product Discontinuation"}]},
    {"name": "Product Returns", "source": "quality", "locator": "Noise Cancelling Headphones",
     "description": "Customers sending a product back after delivery (order status Returned in the sales database)."},
    {"name": "Product Discontinuation", "source": "quality", "locator": "Portable SSD 1TB",
     "description": "A product no longer reordered; is_discontinued = 1 in the sales database."},
]

PASSAGES: dict[str, list[dict[str, Any]]] = {
    "midwest": [
        {"heading": "Summary", "text": "Midwest orders fell behind last year twice in 2024: in January and February, and again in September. "
         "Customers in Ohio and Michigan were hit hardest; Illinois less so.", "terms": ["Midwest Sales Decline 2024"]},
        {"heading": "January cutover", "text": "On January 8, 2024 Midwest fulfilment moved from our Columbus warehouse to Lakeshore "
         "Logistics, a third-party distributor.", "terms": ["Fulfilment Cutover", "Lakeshore Logistics"]},
        {"heading": "January cutover", "text": "For about six weeks delivery took eight days instead of three. Several repeat customers "
         "told support they ordered elsewhere until deliveries recovered.", "terms": ["Delivery Delay", "Fewer Repeat Orders"]},
        {"heading": "September stockout", "text": "Lakeshore ran out of Standing Desk and Office Chair stock in the first two weeks of "
         "September after a missed reorder.", "terms": ["September Stockout", "Lakeshore Logistics"]},
        {"heading": "Decisions", "kind": "summary", "text": "Ohio and Michigan fulfilment moves back to Columbus by January 31, 2025; "
         "Midwest customers who stopped ordering get a win-back offer; Lakeshore sends a weekly furniture stock report.",
         "terms": ["Fulfilment Cutover", "Fewer Repeat Orders"]},
    ],
    "tags": [
        {"heading": "How tags are assigned", "text": "Buyers assign tags by hand when a product is listed and at each quarterly "
         "review. Tags decide placement on the website and in stores; they are not calculated from sales.", "terms": ["Merchandising Tag"]},
        {"heading": "bestseller", "text": "A product the buyers want to feature on the home page. It is a placement decision and "
         "does not mean the product sells the most units.", "terms": ["Bestseller Tag"]},
        {"heading": "clearance", "text": "The tag does not change the price by itself: discounts come from campaigns and apply to "
         "tagged and untagged products alike.", "terms": ["Clearance Tag"]},
    ],
    "quality": [
        {"heading": "Noise Cancelling Headphones", "text": "The most common complaint was ear cushions that cracked after a few months, "
         "and it was the product customers sent back most often.", "terms": ["Ear Cushion Cracking", "Product Returns"]},
        {"heading": "Office Chair", "text": "Customers reported a wobbling base on chairs from one production batch. Several chairs "
         "came back before support started sending replacement bases in June.", "terms": ["Wobbling Chair Base", "Product Returns"]},
        {"heading": "Portable SSD 1TB", "text": "The supplier ended production, so the drive is discontinued from January 2025 and "
         "remaining stock is sold through December 2024.", "terms": ["SSD Heat Slowdown", "Product Discontinuation"]},
        {"heading": "Camping Stove", "text": "The supplier stopped making the stove's gas valve. We are selling the remaining stock "
         "and will not reorder.", "terms": ["Stove Valve Shortage", "Product Discontinuation"]},
    ],
}

ACTION_ITEMS = [
    {"title": "Move Ohio and Michigan fulfilment back to Columbus", "owner": "Min Lee", "due_on": "2025-01-31", "status": "open",
     "region": "Midwest", "_locator": "Decisions", "_evidence": "Move Ohio and Michigan fulfilment back to Columbus by January 31, 2025. Owner: Min Lee."},
    {"title": "Send a win-back offer to Midwest customers", "owner": "Dana Whitfield", "status": "open", "region": "Midwest",
     "_locator": "Decisions", "_evidence": "Send a win-back offer to Midwest customers who have not ordered since August. Owner: Dana Whitfield.",
     "_note": "No due date in the meeting note."},
    {"title": "Weekly furniture stock report from Lakeshore", "owner": "Min Lee", "status": "done", "region": "Midwest",
     "_locator": "Decisions", "_evidence": "Start a weekly furniture stock report from Lakeshore. Owner: Min Lee. Done in October."},
]


def seed_demo_knowledge(svc: "OntologyService") -> dict[str, Any]:
    """Store the demo documents as a host model would; running it again changes nothing."""
    k = svc.knowledge
    k.ds()
    source_ids: dict[str, int] = {}
    passages = 0
    for key, src in SOURCES.items():
        out = k.save_passages(PASSAGES[key], source=src)
        source_ids[key] = out["source_id"]
        passages += len(PASSAGES[key])
    for c in CONCEPTS:
        k.save_concept(
            c["name"], c.get("description"), synonyms=c.get("synonyms"), relations=c.get("relations"),
            source=source_ids[c["source"]], locator=c.get("locator"), evidence=c.get("evidence"),
        )
    k.define_entity(
        "Action Item",
        [
            {"name": "title", "type": "text", "required": True},
            {"name": "owner", "type": "text", "description": "Person responsible, as named in the meeting note."},
            {"name": "due_on", "type": "date"},
            {"name": "status", "type": "text", "description": "open or done"},
            {"name": "region", "type": "text"},
        ],
        "A decision someone has to carry out, from meeting notes.",
        table="action_items",
        keys=["title"],
        synonyms=["Decision", "To-do"],
    )
    k.add_records("Action Item", ACTION_ITEMS, source=source_ids["midwest"])
    return {
        "sources": len(source_ids),
        "passages": passages,
        "concepts": len(CONCEPTS),
        "action_items": len(ACTION_ITEMS),
        "originals": str(k.originals_dir),
    }


def remove_demo_knowledge(svc: "OntologyService") -> dict[str, Any]:
    """Delete what seed_demo_knowledge added: demo:// sources, their passages, records, concepts and originals."""
    k = svc.knowledge
    ds = k.ds(create=False)
    if ds is None or not k.path.exists():
        return {"removed_sources": 0}
    with k._write() as con:
        demo = [dict(r) for r in con.execute("SELECT id, original FROM sources WHERE uri LIKE ?", (f"{DEMO_URI}%",))]
        ids = [s["id"] for s in demo]
        if not ids:
            return {"removed_sources": 0}
        marks = ",".join("?" for _ in ids)
        passage_ids = [r[0] for r in con.execute(f"SELECT id FROM passages WHERE source_id IN ({marks})", ids)]
    if passage_ids:
        k.save_passages(delete_ids=passage_ids)

    entity = next((t for t in svc.store.terms(ds["id"]) if t["name"] == "Action Item" and t.get("spec")), None)
    if entity is not None:
        table = entity["spec"]["table"]
        with k._write() as con:
            others = con.execute(f'SELECT COUNT(*) FROM "{table}" WHERE _source_id IS NULL OR _source_id NOT IN ({marks})', ids).fetchone()[0]
            demo_rows = [r[0] for r in con.execute(f'SELECT id FROM "{table}" WHERE _source_id IN ({marks})', ids)]
        if others == 0:
            k.define_entity("Action Item", delete=True, force=True)
        elif demo_rows:
            k.edit_records("Action Item", "delete", demo_rows)

    demo_names = {c["name"] for c in CONCEPTS} | {r["target"] for c in CONCEPTS for r in c.get("relations") or []}
    removed_concepts = []
    for t in svc.store.terms(ds["id"]):
        if t["name"] not in demo_names or t.get("spec"):
            continue
        cited = {r["source_id"] for r in svc.store.all("SELECT source_id FROM term_source WHERE term_id = ?", (t["id"],))}
        if cited <= set(ids):
            svc.update_term(t["id"], delete=True, datasource=ds["name"])
            removed_concepts.append(t["name"])

    with svc.store.tx() as con:
        con.execute(f"DELETE FROM term_source WHERE source_id IN ({marks})", ids)
        con.execute(f"UPDATE term_relation SET source_id = NULL WHERE source_id IN ({marks})", ids)
    k.index.sync(force=True)  # the Term mirror still points at the demo sources until it is rebuilt
    with k._write() as con:
        con.execute(f"DELETE FROM sources WHERE id IN ({marks})", ids)

    removed_files = []
    for s in demo:
        path = k.vault_root / s["original"] if s.get("original") else None
        if path and path.is_file():
            head = path.read_text(encoding="utf-8", errors="replace")[:600]
            if f"saved_by: {SAVED_BY}" in head and f"uri: {DEMO_URI}" in head:
                path.unlink()
                removed_files.append(s["original"])
    k._refresh(ds, ("sources",))
    svc.store.log("demo", None, "remove", {"sources": len(ids), "passages": len(passage_ids)})
    k.index.sync()
    out = {"removed_sources": len(ids), "removed_passages": len(passage_ids), "removed_concepts": sorted(removed_concepts),
           "removed_originals": removed_files}
    index = k.wiki_dir / "index.md"
    if index.is_file() and f"generated_by: {SAVED_BY}" in index.read_text(encoding="utf-8", errors="replace")[:400]:
        out["removed_pages"] = svc.export_wiki()["removed_stale_pages"]  # prune the demo's exported pages
    return out
