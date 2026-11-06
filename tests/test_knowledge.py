"""The writable personal knowledge store."""

from __future__ import annotations

import sqlite3
from dataclasses import replace

import pytest
from fastmcp import Client

from auto_ontology_mcp.errors import KnowledgeError, OntologyError
from auto_ontology_mcp.knowledge import coerce
from auto_ontology_mcp.server import build_server
from auto_ontology_mcp.service import OntologyService

RECEIPT_IMG = {"title": "IMG_2041.jpg", "kind": "image", "summary": "Two lunch receipts"}


@pytest.fixture()
def ks(settings) -> OntologyService:
    svc = OntologyService(settings)
    k = svc.knowledge
    k.define_entity("Merchant", [{"name": "name", "type": "text", "required": True}, {"name": "city", "type": "text"}],
                    "A shop or restaurant.", keys=["name"])
    k.define_entity(
        "Receipt",
        [
            {"name": "paid_on", "type": "date", "required": True},
            {"name": "merchant", "type": "ref:Merchant"},
            {"name": "category", "type": "text"},
            {"name": "total_krw", "type": "integer", "description": "Total paid in KRW"},
        ],
        "One paid receipt.",
        table="receipts",
        synonyms=["영수증"],
    )
    k.add_records(
        "Receipt",
        [
            {"paid_on": "2026/10/03", "merchant": "을지로 국수집", "category": "Eating Out", "total_krw": "12,000원", "_evidence": "합계 12,000"},
            {"paid_on": "2026-10-05", "merchant": "Chipotle", "category": "Eating Out", "total_krw": 15500, "_locator": "line 3"},
            {"paid_on": "2026-10-09", "merchant": "Chipotle", "category": "Groceries", "total_krw": 40200},
        ],
        source=RECEIPT_IMG,
    )
    return svc


@pytest.mark.parametrize(
    "value,ctype,expected",
    [
        ("12,000원", "integer", 12000),
        ("$1,234.50", "number", 1234.5),
        ("2026/10/3", "date", "2026-10-03"),
        ("2026-10-03 14:30", "datetime", "2026-10-03T14:30:00"),
        ("예", "boolean", 1),
        ("", "text", None),
    ],
)
def test_coerce(value, ctype, expected):
    assert coerce(value, ctype) == expected


@pytest.mark.parametrize("value,ctype", [("12%", "number"), ("yesterday", "date"), ("1.5", "integer"), ("maybe", "boolean")])
def test_coerce_rejects(value, ctype):
    with pytest.raises(KnowledgeError):
        coerce(value, ctype)


def test_store_is_created_on_first_use(settings):
    svc = OntologyService(settings)
    assert svc.store.datasource_by_name("knowledge") is None
    svc.knowledge.save_concept("Zettelkasten", "A note-taking method of atomic, linked notes.")
    ready = {d["datasource"]: d for d in svc.check_readiness()["datasources"]}
    assert ready["knowledge"]["verdict"] == "ready"
    assert ready["knowledge"]["knowledge"]["concepts"] == 1


def test_entities_become_terms_tables_and_relationships(ks):
    term = ks.get_term("영수증", datasource="knowledge")
    assert term["name"] == "Receipt" and term["table"] == "receipts"
    assert {a["name"] for a in term["attributes"]} >= {"Paid On", "Merchant", "Total Krw"}
    assert term["knowledge"]["records"] == 3 and term["knowledge"]["records_with_source"] == 3
    rels = {(r["source"], r["target"]) for r in ks.list_relationships(status="accepted", datasource="knowledge")["relationships"]}
    assert ("receipts.merchant_id", "merchant.id") in rels
    assert ("receipts._source_id", "sources.id") in rels


def test_add_records_normalizes_dedupes_and_creates_references(ks):
    k = ks.knowledge
    rows = ks.run_sql(
        "SELECT r.paid_on, m.name, r.total_krw, s.title, r._evidence FROM receipts r JOIN merchant m ON m.id = r.merchant_id "
        "JOIN sources s ON s.id = r._source_id ORDER BY r.id",
        datasource="knowledge",
    )["rows"]
    assert rows[0] == ["2026-10-03", "을지로 국수집", 12000, "IMG_2041.jpg", "합계 12,000"]
    assert ks.run_sql("SELECT COUNT(*) FROM merchant", datasource="knowledge")["rows"] == [[2]]

    again = k.add_records("Receipt", [{"paid_on": "2026-10-05", "merchant": "chipotle", "category": "Eating Out", "total_krw": 15500}], source=RECEIPT_IMG)
    assert again["inserted"] == [] and again["unchanged"] == [2] and again["source_id"] == 1

    merchant = k.add_records("Merchant", [{"name": "CHIPOTLE", "city": "Austin"}])
    assert merchant["updated"] == [2]


def test_add_records_is_all_or_nothing(ks):
    with pytest.raises(KnowledgeError) as exc:
        ks.knowledge.add_records("Receipt", [
            {"paid_on": "2026-10-10", "total_krw": 1000},
            {"paid_on": "last tuesday", "total_krw": "10%", "tip": 3},
        ])
    message = str(exc.value)
    assert "record 2: paid_on" in message and "record 2: total_krw" in message and "unknown column 'tip'" in message
    assert ks.run_sql("SELECT COUNT(*) FROM receipts", datasource="knowledge")["rows"] == [[3]]
    with pytest.raises(KnowledgeError, match="required"):
        ks.knowledge.add_records("Receipt", [{"total_krw": 1000}])


def test_keys_upsert(settings):
    svc = OntologyService(settings)
    k = svc.knowledge
    k.define_entity("Book", [{"name": "isbn", "type": "text"}, {"name": "title", "type": "text"}, {"name": "rating", "type": "integer"}], keys=["isbn"])
    k.add_records("Book", [{"isbn": "978-0", "title": "Make It Stick", "rating": 4}])
    out = k.add_records("Book", [{"isbn": "978-0", "rating": 5}])
    assert out["updated"] == [1]
    assert svc.run_sql("SELECT title, rating FROM book", datasource="knowledge")["rows"] == [["Make It Stick", 5]]
    with pytest.raises(KnowledgeError):
        k.add_records("Book", [{"isbn": "978-0", "rating": 3}], on_conflict="error")


def test_entity_evolves_and_protects_types(ks):
    k = ks.knowledge
    out = k.define_entity("Receipt", [{"name": "payment_method", "type": "text"}])
    assert out["added_columns"] == ["payment_method"] and out["records"] == 3
    k.add_records("Receipt", [{"id": 1, "payment_method": "card"}])
    assert ks.run_sql("SELECT payment_method FROM receipts WHERE id = 1", datasource="knowledge")["rows"] == [["card"]]
    with pytest.raises(KnowledgeError, match="types cannot change"):
        k.define_entity("Receipt", [{"name": "total_krw", "type": "text"}])
    with pytest.raises(KnowledgeError, match="snake_case"):
        k.define_entity("운동 기록", [{"name": "distance_km", "type": "number"}])
    assert k.define_entity("운동 기록", [{"name": "distance_km", "type": "number"}], table="workouts")["table"] == "workouts"


def test_edit_and_delete(ks):
    k = ks.knowledge
    k.edit_records("Receipt", "update", [3], {"category": "Food", "_note": "re-categorized"})
    assert ks.run_sql("SELECT category, _note FROM receipts WHERE id = 3", datasource="knowledge")["rows"] == [["Food", "re-categorized"]]
    with pytest.raises(KnowledgeError, match="still referenced"):
        k.edit_records("Merchant", "delete", [2])
    k.edit_records("Receipt", "delete", [2, 3])
    k.edit_records("Merchant", "delete", [2])
    with pytest.raises(KnowledgeError, match="force"):
        k.define_entity("Receipt", delete=True)
    with pytest.raises(KnowledgeError, match="reference"):
        k.define_entity("Merchant", delete=True, force=True)
    assert k.define_entity("Receipt", delete=True, force=True)["records_deleted"] == 1


def test_concepts_relations_and_sources(ks):
    k = ks.knowledge
    book = {"title": "Make It Stick", "kind": "book", "author": "Brown"}
    out = k.save_concept(
        "Spaced Repetition", "Reviewing material at increasing intervals.",
        body="Reviews are scheduled just before you would forget.",
        relations=[{"relation": "is_a", "target": "Learning Technique"}, {"relation": "related to", "target": "Forgetting Curve"}],
        source=book, locator="ch. 2",
    )
    assert out["stub_concepts_created"] == ["Learning Technique", "Forgetting Curve"]
    assert out["concept"]["sources"][0]["title"] == "Make It Stick"
    curve = ks.get_term("Forgetting Curve", datasource="knowledge")
    assert curve["relations"] == [{"direction": "incoming", "relation": "related_to", "term": "Spaced Repetition", "source_id": 2}]

    k.save_concept("Spaced Repetition", append_body="Anki implements it.", status="certified")
    k.save_concept("Spaced Repetition", append_body="Leitner boxes are a paper version.")
    term = ks.get_term("Spaced Repetition", datasource="knowledge")
    assert term["status"] == "draft" and term["body"].endswith("paper version.")

    hits = ks.search_ontology("leitner boxes", datasource="knowledge")["hits"]
    assert hits[0]["name"] == "Spaced Repetition" and hits[0]["term_kind"] == "concept"
    assert ks.search_ontology("make it stick", datasource="knowledge")["hits"][0]["kind"] == "source"

    src = k.get_source("Make It Stick")
    assert src["concepts"][0]["name"] == "Spaced Repetition"
    assert {r["relation"] for r in src["relations"]} == {"is_a", "related_to"}
    k.save_concept("Spaced Repetition", remove_relations=[{"relation": "is_a", "target": "Learning Technique"}])
    assert len(ks.get_term("Spaced Repetition", datasource="knowledge")["relations"]) == 1

    stats = ks.check_readiness()
    know = next(d for d in stats["datasources"] if d["datasource"] == "knowledge")["knowledge"]
    assert know["stub_concepts"] == ["Forgetting Curve", "Learning Technique"]


def test_answering_from_the_store(ks):
    verdict = ks.check_answerable("10월 외식비 얼마야?", ["영수증", "Eating Out"], datasource="knowledge")
    assert verdict["verdict"] == "answerable"
    ctx = ks.get_query_context(["Receipt", "Merchant"], datasource="knowledge")
    assert ctx["join_path"]["hops"][0]["on"] == "receipts.merchant_id = merchant.id"
    warnings = ks.validate_sql("SELECT SUM(total_krw) FROM receipts WHERE category = 'eating out'", datasource="knowledge")["warnings"]
    assert "'Eating Out'" in warnings[0]
    out = ks.run_sql("SELECT SUM(total_krw) FROM receipts WHERE category = 'Eating Out' AND paid_on LIKE '2026-10-%'", datasource="knowledge")
    assert out["rows"] == [[27500]]
    assert not out["grounding"]["unmodeled_columns"]


def test_run_sql_still_cannot_write_the_store(ks):
    with pytest.raises(OntologyError):
        ks.run_sql("DELETE FROM receipts", datasource="knowledge")
    with pytest.raises(Exception, match="readonly"):
        ks.connections.run_readonly(ks.ds("knowledge"), "DELETE FROM receipts")


def test_knowledge_export_import_roundtrip(ks, settings, tmp_path):
    ks.knowledge.save_concept("Eating Out", "Restaurant and takeout spending.", relations=[{"relation": "part_of", "target": "Living Costs"}])
    doc = ks.export_ontology(datasource="knowledge")["document"]
    other_home = tmp_path / "other"
    fresh = OntologyService(replace(settings, home=other_home, store_path=other_home / "o.db"))
    fresh.knowledge.ds()
    report = fresh.import_ontology(doc, datasource="knowledge")
    assert not report["skipped"], report
    assert fresh.get_term("Receipt", datasource="knowledge")["table"] == "receipts"
    assert fresh.get_term("Eating Out", datasource="knowledge")["relations"][0]["term"] == "Living Costs"


def test_store_migration_adds_columns(tmp_path):
    from auto_ontology_mcp.store import Store

    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript(
        "CREATE TABLE term (id TEXT PRIMARY KEY, datasource_id TEXT NOT NULL, name TEXT NOT NULL, description TEXT, synonyms TEXT,"
        " status TEXT NOT NULL DEFAULT 'draft', primary_table_id TEXT, note TEXT, created_at TEXT NOT NULL,"
        " updated_at TEXT NOT NULL, certified_at TEXT, UNIQUE (datasource_id, name));"
    )
    con.close()
    Store(path)
    cols = {r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(term)")}
    assert {"kind", "body", "spec"} <= cols


async def test_knowledge_tools_over_mcp(settings):
    svc = OntologyService(settings)
    async with Client(build_server(settings, svc)) as client:
        tools = {t.name: t for t in await client.list_tools()}
        assert {"define_entity", "add_records", "edit_records", "save_concept", "get_source"} <= set(tools)
        assert tools["get_source"].annotations.read_only_hint is True
        await client.call_tool("define_entity", {"name": "Workout", "table": "workouts", "columns": [
            {"name": "done_on", "type": "date"}, {"name": "distance_km", "type": "number"}]})
        added = await client.call_tool("add_records", {
            "entity": "Workout", "records": [{"done_on": "2026-11-01", "distance_km": "5.2"}],
            "source": {"title": "Strava export", "kind": "csv"}})
        assert added.data["inserted"] == [1]
        concept = await client.call_tool("save_concept", {"name": "Zone 2", "description": "Easy aerobic training.",
                                                           "relations": [{"relation": "part_of", "target": "Endurance Training"}]})
        assert concept.data["stub_concepts_created"] == ["Endurance Training"]
        found = await client.call_tool("search", {"query": "strava"})
        assert any(r["id"].startswith("source:") for r in found.data["results"])
        fetched = await client.call_tool("fetch", {"id": "source:1"})
        assert "Workout" in fetched.data["text"]


def test_index_log_and_value_search(ks):
    ks.knowledge.save_concept("Eating Out", "Restaurant and takeout spending.", relations=[{"relation": "part_of", "target": "Living Costs"}])
    index = ks.list_terms(datasource="knowledge")
    by_name = {t["name"]: t for t in index["terms"]}
    assert by_name["Receipt"]["records"] == 3 and by_name["Receipt"]["kind"] == "entity"
    assert by_name["Living Costs"]["empty"] is True
    assert {t["name"] for t in ks.list_terms(kind="concept", datasource="knowledge")["terms"]} == {"Eating Out", "Living Costs"}

    log = [c["what"] for c in ks.recent_changes()["changes"]]
    assert log[0].startswith("concept Eating Out: create")
    assert any("Receipt: records add (3 inserted)" in w for w in log)
    assert not any("scan" in w for w in log)

    hits = ks.search_ontology("chipotle", datasource="knowledge")["hits"]
    value = next(h for h in hits if h["kind"] == "value")
    assert value["column"] == "merchant.name" and value["values"] == ["Chipotle"]


def test_concept_questions_point_to_get_term(ks):
    ks.knowledge.save_concept("Spaced Repetition", "Reviewing material at increasing intervals.")
    out = ks.check_answerable("What is spaced repetition?", ["spaced repetition"], datasource="knowledge")
    assert out["verdict"] == "answerable" and "get_term" in out["next_step"]


def test_source_content_is_searchable(ks):
    ks.knowledge.save_concept("Interleaving", "Mixing problem types while practicing.",
                              source={"title": "Make It Stick", "kind": "book", "content": "Interleaving beats blocked practice for retention."})
    hits = ks.search_ontology("blocked practice", datasource="knowledge")["hits"]
    assert any(h["kind"] == "source" and h["name"] == "Make It Stick" for h in hits)


def test_export_wiki_writes_an_obsidian_vault(ks, tmp_path):
    k = ks.knowledge
    k.save_concept("Eating Out", "Restaurant and takeout spending.", body="Includes delivery apps.",
                   synonyms=["외식"], relations=[{"relation": "part_of", "target": "Living Costs"}],
                   source={"title": "Budget notes", "kind": "note"})
    out = ks.export_wiki(directory=str(tmp_path / "vault"))
    vault = tmp_path / "vault"
    page = (vault / "Eating Out.md").read_text()
    assert page.startswith("---\ngenerated_by: auto-ontology-mcp")
    assert "aliases:\n- 외식" in page and "part of [[Living Costs]]" in page and "Includes delivery apps." in page
    receipt = (vault / "Receipt.md").read_text()
    assert "## Records (3)" in receipt
    assert "| 을지로 국수집 |" in receipt and "IMG_2041.jpg]]" in receipt  # references and sources shown by name
    assert "[[Source]]" not in receipt and not (vault / "Source.md").exists()
    assert "[[Merchant]] via `receipts.merchant_id = merchant.id`" in receipt
    assert "[[Eating Out]]" in (vault / "index.md").read_text()
    assert (vault / "sources" / "1 - IMG_2041.jpg.md").exists()
    assert "Receipt: records add" in (vault / "log.md").read_text()

    (vault / "my own note.md").write_text("not generated")
    ks.update_term("Living Costs", delete=True, datasource="knowledge")
    again = ks.export_wiki(directory=str(vault))
    assert "Living Costs.md" in again["removed_stale_pages"]
    assert (vault / "my own note.md").exists()
    assert out["pages"] >= 6


def test_observed_joins_feed_relationship_review(modeled):
    modeled.run_sql("SELECT COUNT(*) FROM orders o JOIN employees e ON e.employee_id = o.sales_rep_id AND 1 = 1")
    svc = modeled
    seen = svc.list_relationships()["joins_seen_in_queries"]
    assert seen == []  # sales_rep_id is already a manual relationship in the fixture
    svc.run_sql("SELECT COUNT(*) FROM cities c JOIN regions r ON r.region_id = c.state_id")
    seen = svc.list_relationships()["joins_seen_in_queries"]
    assert seen[0]["join"] in ("cities.state_id = regions.region_id", "regions.region_id = cities.state_id")
