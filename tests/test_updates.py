"""When a source changes: a document saved again, or a database whose schema changed since it was modeled."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastmcp import Client

from auto_ontology_mcp.errors import KnowledgeError
from auto_ontology_mcp.server import build_server
from auto_ontology_mcp.service import OntologyService

V1 = "# Refunds\n\nRefunds take 14 days.\n\n# Shipping\n\nOrders ship within 2 days."
V2 = "# Refunds\n\nRefunds take 30 days.\n\n# Shipping\n\nOrders ship within 2 days."
HANDBOOK = {"title": "Team handbook", "kind": "doc", "uri": "file:///docs/handbook.md"}


@pytest.fixture()
def k(settings):
    return OntologyService(settings).knowledge


def _save(k, version: str, passages: list[str], **kwargs):
    items = [{"text": text, "heading": text.split()[0], "terms": ["Refund Policy"] if "Refund" in text else []} for text in passages]
    return k.save_passages(items, source={**HANDBOOK, "original_text": version}, **kwargs)


def test_changed_document_replaces_its_passages_and_original(k):
    first = _save(k, V1, ["Refunds take 14 days.", "Orders ship within 2 days."])
    assert first["original"] == "originals/Team handbook.md"
    old_note = k.vault_root / first["original"]
    # The user annotates the original in Obsidian; saving the same document again is still no change.
    old_note.write_text(old_note.read_text() + "\nMy note: ask finance about this.\n")
    again = _save(k, V1, ["Refunds take 14 days.", "Orders ship within 2 days."])
    assert again["original"] == first["original"] and "originals_replaced" not in again
    assert again["unchanged"] == first["inserted"]

    out = _save(k, V2, ["Refunds take 30 days.", "Orders ship within 2 days."], replace=True)
    shipping, refund_v1 = first["inserted"][1], first["inserted"][0]
    assert out["unchanged"] == [shipping] and out["deleted"] == [refund_v1] and len(out["inserted"]) == 1
    assert out["original"] == "originals/Team handbook (2).md"
    assert out["originals_replaced"] == [
        {"source_id": out["source_id"], "title": "Team handbook", "previous": "originals/Team handbook.md",
         "original": "originals/Team handbook (2).md"}
    ]
    assert "My note: ask finance" in old_note.read_text()  # the old version stays as the user left it

    src = k.get_source(out["source_id"])
    assert [p["text"] for p in src["passages"]] == ["Orders ship within 2 days.", "Refunds take 30 days."]
    assert src["original_link"] == "[[originals/Team handbook (2)|Team handbook]]"
    assert k.stats()["sources"] == 1
    assert not k.search_text("14", kinds=["passage"])["hits"]
    hit = k.search_text("30 days", kinds=["passage"])["hits"][0]
    assert hit["link"] == "[[originals/Team handbook (2)#Refunds]]" and hit["terms"] == ["Refund Policy"]
    log = [c["what"] for c in k.svc.recent_changes(limit=20)["changes"]]
    assert "source Team handbook: new original originals/Team handbook (2).md (previous originals/Team handbook.md kept)" in log


def test_without_replace_old_passages_stay(k):
    _save(k, V1, ["Refunds take 14 days."])
    out = _save(k, V2, ["Refunds take 30 days."])
    assert out["deleted"] == []
    assert {p["text"] for p in k.get_source(out["source_id"])["passages"]} == {"Refunds take 14 days.", "Refunds take 30 days."}


def test_replace_needs_a_source_and_its_passages(k):
    with pytest.raises(KnowledgeError, match="needs the source whose passages"):
        k.save_passages([{"id": 1, "text": "x"}], replace=True)
    with pytest.raises(KnowledgeError, match="needs the source's current passages"):
        k.save_passages(None, source=HANDBOOK, delete_ids=[1], replace=True)


def test_replace_leaves_other_sources_alone(k):
    _save(k, V1, ["Refunds take 14 days."])
    other = k.save_passages([{"text": "Refunds are free."}], source={"title": "FAQ", "kind": "web"})
    _save(k, V2, ["Refunds take 30 days."], replace=True)
    assert [p["text"] for p in k.get_source(other["source_id"])["passages"]] == ["Refunds are free."]


def test_a_note_the_user_linked_is_not_replaced(k):
    note = k.vault_root / "Clippings" / "Handbook.md"
    note.parent.mkdir(parents=True)
    note.write_text(V1)
    first = k.save_passages([{"text": "Refunds take 14 days."}], source={**HANDBOOK, "original": "Clippings/Handbook.md"})
    out = k.save_passages([{"text": "Refunds take 30 days."}], source={**HANDBOOK, "original_text": V2})
    assert first["original"] == out["original"] == "Clippings/Handbook.md" and "originals_replaced" not in out
    assert not (k.originals_dir / "Team handbook.md").exists()


def test_changed_file_is_copied_next_to_the_old_one(k, tmp_path):
    pdf = tmp_path / "inbox" / "Handbook.pdf"
    pdf.parent.mkdir()
    pdf.write_bytes(b"%PDF-1.4 version one")
    source = {**HANDBOOK, "original_file": str(pdf)}
    first = k.save_passages([{"text": "Refunds take 14 days."}], source=source)
    assert k.save_passages([{"text": "Refunds take 14 days."}], source=source)["original"] == first["original"]
    pdf.write_bytes(b"%PDF-1.4 version two")
    out = k.save_passages([{"text": "Refunds take 30 days."}], source=source, replace=True)
    assert (first["original"], out["original"]) == ("originals/Handbook.pdf", "originals/Handbook (2).pdf")
    assert (k.vault_root / first["original"]).read_bytes() == b"%PDF-1.4 version one"
    assert [p["text"] for p in k.get_source(out["source_id"])["passages"]] == ["Refunds take 30 days."]


def test_records_and_concepts_report_a_replaced_original(k):
    k.define_entity("Policy", columns=[{"name": "topic", "type": "text"}, {"name": "days", "type": "integer"}], keys=["topic"])
    k.add_records("Policy", [{"topic": "refund", "days": 14}], source={**HANDBOOK, "original_text": V1})
    out = k.add_records("Policy", [{"topic": "refund", "days": 30}], source={**HANDBOOK, "original_text": V2})
    assert out["updated"] and out["originals_replaced"][0]["original"] == "originals/Team handbook (2).md"
    concept = k.save_concept("Refund Policy", "Refunds take 30 days.", source={**HANDBOOK, "original_text": V2})
    assert "originals_replaced" not in concept  # already the current version


async def test_replace_over_mcp(settings):
    svc = OntologyService(settings)
    async with Client(build_server(settings, svc)) as client:
        source = {**HANDBOOK, "original_text": V1}
        await client.call_tool("save_passages", {"passages": [{"text": "Refunds take 14 days."}], "source": source})
        out = await client.call_tool("save_passages", {
            "passages": [{"text": "Refunds take 30 days."}], "source": {**HANDBOOK, "original_text": V2}, "replace": True,
        })
        assert out.data["deleted"] == [1] and out.data["original"] == "originals/Team handbook (2).md"


# ---------------------------------------------------------------- databases
def test_rescan_returns_modeled_tables_that_gained_columns(modeled: OntologyService, demo_path: Path):
    svc = modeled
    svc.update_term("Product", status="certified", datasource="demo")
    with sqlite3.connect(demo_path) as con:
        con.execute("ALTER TABLE products ADD COLUMN warranty_months INTEGER")
        con.execute("CREATE TABLE coupons (coupon_id INTEGER PRIMARY KEY, code TEXT)")

    scan = svc.scan_datasource("demo")
    assert scan["tables_without_terms"] == 1 and scan["new_columns_on_modeled_tables"] == {"products": ["warranty_months"]}
    demo = next(d for d in svc.check_readiness()["datasources"] if d["datasource"] == "demo")
    assert demo["verdict"] == "partially_modeled"
    assert demo["tables_without_terms"] == ["coupons"] and demo["tables_with_new_columns"] == ["products"]

    work = svc.get_modeling_worklist("demo", limit=10)
    assert work["progress"]["remaining"] == 2 and work["progress"]["modeled_with_new_columns"] == 1
    assert [t["table"] for t in work["tables"]] == ["coupons", "products"]  # new tables first
    products = work["tables"][1]
    assert products["new_columns"] == ["warranty_months"]
    (current,) = products["current_terms"]
    assert current["name"] == "Product" and "warranty_months" not in [c["column"] for c in current["columns"]]
    assert "current_terms" in work["next_step"]

    # Send the current Terms back with the new column placed.
    current["columns"].append({"column": "warranty_months", "display_name": "Warranty Months"})
    saved = svc.save_table_terms("products", [current], datasource="demo")
    assert saved["removed_terms"] == [] and saved["certified_terms_reset_to_draft"] == []
    svc.save_table_terms("coupons", [{"name": "Coupon", "description": "A discount code.", "columns": [
        {"column": "coupon_id", "display_name": "Coupon ID"}, {"column": "code", "display_name": "Code"}]}], datasource="demo")

    work = svc.get_modeling_worklist("demo")
    assert work["progress"]["remaining"] == 0 and work["tables"] == []
    assert next(d for d in svc.check_readiness()["datasources"] if d["datasource"] == "demo")["verdict"] == "ready"
    assert svc.get_term("Product", datasource="demo")["status"] == "certified"
    assert "warranty_months" in str(svc.get_query_context(["Product"], datasource="demo"))


def test_knowledge_entities_are_not_reported_as_new_columns(k):
    k.define_entity("Workout", columns=[{"name": "minutes", "type": "integer"}])
    k.add_records("Workout", [{"minutes": 30}], source={"title": "Watch export", "kind": "file"})
    k.define_entity("Workout", columns=[{"name": "calories", "type": "integer"}])
    know = next(d for d in k.svc.check_readiness()["datasources"] if d["datasource"] == "knowledge")
    assert know["tables_with_new_columns"] == []
