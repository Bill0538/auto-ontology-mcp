"""Passages, full-text search, originals in the vault and SQL over the concept graph."""

from __future__ import annotations

import sqlite3

import pytest
from fastmcp import Client

from auto_ontology_mcp.errors import KnowledgeError, OntologyError
from auto_ontology_mcp.server import build_server
from auto_ontology_mcp.service import OntologyService
from auto_ontology_mcp.textindex import query_words

ARTICLE = """# 기억의 과학

## 망각 곡선
에빙하우스의 망각 곡선은 시간이 지나면 기억이 빠르게 사라진다는 것을 보여 준다.

## 간격 반복
간격 반복은 망각곡선을 이용한 학습 기법이다. 복습 간격을 점점 늘린다.
"""


@pytest.fixture()
def kb(settings) -> OntologyService:
    svc = OntologyService(settings)
    k = svc.knowledge
    k.save_concept("Learning Technique", "A method for learning more effectively.")
    k.save_concept("Spaced Repetition", "Reviewing at increasing intervals.", synonyms=["간격 반복"],
                   relations=[{"relation": "is_a", "target": "Learning Technique"}])
    k.save_concept("Leitner System", "Flashcards in boxes reviewed less often as you get them right.",
                   relations=[{"relation": "is_a", "target": "Spaced Repetition"}])
    k.save_concept("Forgetting Curve", "Memory decays quickly without review.", synonyms=["망각 곡선"],
                   relations=[{"relation": "related_to", "target": "Spaced Repetition"}])
    source = {"title": "기억의 과학", "kind": "web", "uri": "https://example.com/memory", "original_text": ARTICLE}
    k.save_passages(
        [
            {"text": "에빙하우스의 망각 곡선은 시간이 지나면 기억이 빠르게 사라진다는 것을 보여 준다.",
             "heading": "망각 곡선", "terms": ["Forgetting Curve"]},
            {"text": "간격 반복은 망각곡선을 이용한 학습 기법이다.", "heading": "간격 반복", "terms": ["Spaced Repetition", "Learning Technique"]},
            {"text": "복습 간격을 점점 늘리는 것이 핵심이다.", "kind": "summary", "heading": "간격 반복", "terms": ["Spaced Repetition"]},
        ],
        source=source,
    )
    return svc


def test_query_words_drop_korean_particles():
    assert query_words("망각곡선을 기억") == ["망각곡선", "기억"]
    assert query_words("학습에서 효과") == ["학습", "효과"]
    assert query_words("Spaced spaced repetition!") == ["Spaced", "repetition"]


def test_search_text_finds_korean_words_particles_and_compounds(kb):
    k = kb.knowledge
    two_char = k.search_text("기억", kinds=["passage"])["hits"]  # 2-character word inside "기억이"
    assert [h["heading"] for h in two_char] == ["망각 곡선"]
    assert "[기억이]" in two_char[0]["snippet"] and two_char[0]["match"] == "words"

    compound = k.search_text("곡선", kinds=["passage"])["hits"]  # also inside the compound 망각곡선을
    assert {h["match"] for h in compound} == {"words", "substring"}
    assert len(compound) == 2

    hit = k.search_text("망각 곡선", kinds=["passage"])["hits"][0]
    assert hit["terms"] == ["Forgetting Curve"] and hit["source"] == "기억의 과학"
    assert hit["link"] == "[[originals/기억의 과학#망각 곡선]]"

    terms = k.search_text("flashcards boxes", kinds=["term"])["hits"]
    assert terms[0]["name"] == "Leitner System"
    only = k.search_text("간격", term="Spaced Repetition")["hits"]
    assert only and all(h["kind"] == "passage" and "Spaced Repetition" in h["terms"] for h in only)
    out = k.search_text("nothing like this anywhere")
    assert out["hits"] == [] and out["hint"]


def test_search_falls_back_to_any_word(kb):
    out = kb.knowledge.search_text("에빙하우스 zebra", kinds=["passage"])
    assert out["matched"] == "any word" and out["hits"][0]["heading"] == "망각 곡선"


def test_sql_walks_the_concept_graph(kb):
    sql = """
        WITH RECURSIVE kinds(id, name, depth) AS (
            SELECT id, name, 0 FROM terms WHERE name = 'Learning Technique'
            UNION ALL
            SELECT r.from_term_id, r.from_term, kinds.depth + 1
            FROM term_relations r JOIN kinds ON r.to_term_id = kinds.id
            WHERE r.relation = 'is_a' AND kinds.depth < 5
        )
        SELECT name, depth FROM kinds ORDER BY depth
    """
    check = kb.validate_sql(sql, datasource="knowledge")
    assert check["valid"], check
    rows = kb.run_sql(sql, datasource="knowledge")["rows"]
    assert rows == [["Learning Technique", 0], ["Spaced Repetition", 1], ["Leitner System", 2]]


def test_sql_combines_full_text_search_with_relations(kb):
    # Passages that mention 간격 and are about a Term that is_a Learning Technique.
    sql = """
        SELECT p.id, s.title, t.name, snippet(passages_fts, -1, '[', ']', '…', 8) AS hit
        FROM passages_fts
        JOIN passages p ON p.id = passages_fts.passage_id
        JOIN sources s ON s.id = p.source_id
        JOIN passage_terms pt ON pt.passage_id = p.id
        JOIN terms t ON t.id = pt.term_id
        JOIN term_relations r ON r.from_term_id = t.id
        WHERE passages_fts MATCH '"간격"*' AND r.relation = 'is_a' AND r.to_term = 'Learning Technique'
        ORDER BY bm25(passages_fts)
    """
    check = kb.validate_sql(sql, datasource="knowledge")
    assert check["valid"] and not check["errors"], check
    assert not [w for w in check["warnings"] if "not how the ontology joins" in w or "relationship" in w.lower()], check["warnings"]
    out = kb.run_sql(sql, datasource="knowledge")
    assert {r[2] for r in out["rows"]} == {"Spaced Repetition"}
    assert len(out["rows"]) == 2 and all("[간격]" in r[3] for r in out["rows"])
    assert not any("passages_fts" in c for c in out["grounding"]["unmodeled_columns"])


def test_mirror_follows_changes(kb):
    kb.update_term("Leitner System", name="Leitner Box", datasource="knowledge")
    rows = kb.run_sql("SELECT from_term FROM term_relations WHERE to_term = 'Spaced Repetition' AND relation = 'is_a'",
                      datasource="knowledge")["rows"]
    assert rows == [["Leitner Box"]]
    kb.update_term("Leitner Box", delete=True, datasource="knowledge")
    assert kb.run_sql("SELECT COUNT(*) FROM terms WHERE name = 'Leitner Box'", datasource="knowledge")["rows"] == [[0]]


def test_passages_dedupe_update_and_delete(kb):
    k = kb.knowledge
    again = k.save_passages([{"text": "간격 반복은 망각곡선을 이용한 학습 기법이다.", "locator": "para 2"}], source={"id": 1})
    assert again["inserted"] == [] and len(again["updated"]) == 1
    pid = again["updated"][0]
    src = k.get_source(1)
    assert src["source"]["original"] == "originals/기억의 과학.md"
    assert next(p for p in src["passages"] if p["id"] == pid)["locator"] == "para 2"
    deleted = k.save_passages(delete_ids=[pid])
    assert deleted["deleted"] == [pid] and deleted["total_passages"] == 2
    assert not k.search_text("이용한", kinds=["passage"])["hits"]
    with pytest.raises(KnowledgeError, match="need a source"):
        k.save_passages([{"text": "orphan"}])
    with pytest.raises(KnowledgeError, match="one idea per passage"):
        k.save_passages([{"text": "x" * 6000}], source={"id": 1})
    with pytest.raises(KnowledgeError, match="No passages with ids"):
        k.save_passages(delete_ids=[999])


def test_stub_concepts_from_passages(kb):
    out = kb.knowledge.save_passages([{"text": "Interleaving mixes problem types.", "terms": ["Interleaving"]}],
                                     source={"title": "Make It Stick", "kind": "book"})
    assert out["stub_concepts_created"] == ["Interleaving"]
    assert kb.get_term("Interleaving", datasource="knowledge")["kind"] == "concept"


def test_originals_are_kept_in_the_vault(kb, tmp_path):
    k = kb.knowledge
    note = k.originals_dir / "기억의 과학.md"
    assert note.read_text().startswith("---\ntitle: 기억의 과학\nsource_id: 1")
    assert "## 간격 반복" in note.read_text()

    # The same text again reuses the note; a different text with the same title gets its own file.
    k.save_passages([{"text": "다른 글의 한 문장."}], source={"title": "기억의 과학", "kind": "pdf", "original_text": "다른 글"})
    assert (k.originals_dir / "기억의 과학 (2).md").exists()

    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4 test")
    out = k.save_passages([{"text": "A finding.", "locator": "p. 3"}], source={"title": "Paper", "kind": "pdf", "original_file": str(pdf)})
    assert out["original"] == "originals/paper.pdf" and (k.originals_dir / "paper.pdf").read_bytes() == pdf.read_bytes()

    hidden = tmp_path / ".secret" / "key.pdf"
    hidden.parent.mkdir()
    hidden.write_bytes(b"x")
    with pytest.raises(KnowledgeError, match="hidden"):
        k.save_passages([{"text": "x"}], source={"title": "Key", "original_file": str(hidden)})
    script = tmp_path / "run.sh"
    script.write_text("echo hi")
    with pytest.raises(KnowledgeError, match="not copied"):
        k.save_passages([{"text": "x"}], source={"title": "Script", "original_file": str(script)})
    with pytest.raises(KnowledgeError, match="outside the Obsidian vault"):
        k.save_passages([{"text": "x"}], source={"title": "Elsewhere", "original": str(tmp_path / "paper.pdf")})

    clip = k.vault_root / "Clippings" / "Article.md"
    clip.parent.mkdir(parents=True)
    clip.write_text("# Article\n\nBody")
    linked = k.save_passages([{"text": "Body", "heading": "Article"}], source={"title": "Article", "kind": "web", "original": "Clippings/Article.md"})
    assert linked["original"] == "Clippings/Article.md"
    assert k.search_text("Body", kinds=["passage"])["hits"][0]["link"] == "[[Clippings/Article#Article]]"


def test_user_vault_setting_puts_pages_in_a_subfolder(settings, tmp_path):
    from dataclasses import replace

    vault = tmp_path / "MyVault"
    (vault).mkdir()
    (vault / "index.md").write_text("my own index")
    svc = OntologyService(replace(settings, vault=vault))
    svc.knowledge.save_passages([{"text": "A line."}], source={"title": "Note", "kind": "note", "original_text": "A line."})
    assert (vault / "auto-ontology" / "originals" / "Note.md").exists()
    out = svc.export_wiki()
    assert out["directory"] == str(vault / "auto-ontology")
    assert (vault / "index.md").read_text() == "my own index"
    page = (vault / "auto-ontology" / "sources" / "1 - Note.md").read_text()
    assert "Original: [[auto-ontology/originals/Note]]" in page


def test_export_shows_passages_and_never_touches_originals(kb, tmp_path):
    k = kb.knowledge
    vault = k.wiki_dir
    (vault / "index.md").parent.mkdir(parents=True, exist_ok=True)
    out = kb.export_wiki()
    assert out["directory"] == str(vault)
    source_page = (vault / "sources" / "1 - 기억의 과학.md").read_text()
    assert "Original: [[originals/기억의 과학]]" in source_page
    assert "## Passages (3)" in source_page
    assert "[[originals/기억의 과학#망각 곡선|망각 곡선]] about [[Forgetting Curve]]" in source_page
    term_page = (vault / "Spaced Repetition.md").read_text()
    assert "## Passages (2)" in term_page and "from [[sources/1 - 기억의 과학|기억의 과학]]" in term_page
    assert not (vault / "Source Passage.md").exists() and not (vault / "Ontology Term.md").exists()
    original = (vault / "originals" / "기억의 과학.md").read_text()
    kb.export_wiki()
    assert (vault / "originals" / "기억의 과학.md").read_text() == original

    (vault / "log.md").write_text("I replaced the log with my own notes")
    again = kb.export_wiki()
    assert "log.md" in again["skipped_user_pages"]
    assert (vault / "log.md").read_text() == "I replaced the log with my own notes"


def test_built_in_tables_are_protected_and_hidden(kb):
    index = kb.list_terms(datasource="knowledge")
    names = {t["name"] for t in index["terms"]}
    assert "Ontology Term" not in names and "Source" not in names and "Spaced Repetition" in names
    assert any(b.startswith("Source Passage (passages)") for b in index["built_in_tables"])
    with pytest.raises(KnowledgeError, match="save_passages"):
        kb.knowledge.add_records("Source Passage", [{"text": "x"}])
    with pytest.raises(KnowledgeError, match="read-only"):
        kb.knowledge.define_entity("Ontology Term", [{"name": "x"}])
    with pytest.raises(KnowledgeError, match="reserved"):
        kb.knowledge.define_entity("Note", [{"name": "body"}], table="passages")
    with pytest.raises(OntologyError, match="built into"):
        kb.update_term("Ontology Relation", name="Links", datasource="knowledge")
    with pytest.raises(KnowledgeError, match="built-in"):
        kb.knowledge.save_concept("Habit", relations=[{"relation": "related_to", "target": "Source Passage"}])


def test_fts_shadow_tables_stay_out_of_the_catalog(kb):
    tables = {t["name"] for t in kb.store.tables(kb.ds("knowledge")["id"])}
    assert {"passages_fts", "terms", "term_relations", "passages"} <= tables
    assert not any(t.endswith(("_data", "_idx", "_docsize", "_config", "_content")) for t in tables)
    ready = next(d for d in kb.check_readiness()["datasources"] if d["datasource"] == "knowledge")
    assert ready["tables_without_terms"] == []
    assert ready["knowledge"]["passages"] == 3 and ready["knowledge"]["sources_with_original"] == 1


def test_search_ontology_adds_text_hits(kb):
    out = kb.search_ontology("복습", datasource="knowledge")
    assert out["text_hits"][0]["kind"] == "passage"


def test_existing_store_is_upgraded(settings):
    svc = OntologyService(settings)
    svc.knowledge.define_entity("Book", [{"name": "title", "type": "text"}])
    con = sqlite3.connect(svc.knowledge.path)
    for (trigger,) in con.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'").fetchall():
        con.execute(f"DROP TRIGGER {trigger}")
    for table in ("passages_fts", "terms_fts", "sources_fts", "passage_terms", "passages", "term_sources", "term_relations", "terms"):
        con.execute(f"DROP TABLE {table}")
    con.execute("ALTER TABLE sources DROP COLUMN original")
    con.commit()
    con.close()
    with svc.store.tx() as c:
        c.execute("DELETE FROM term WHERE name IN ('Source Passage', 'Ontology Term', 'Passage Search Index')")

    fresh = OntologyService(settings)
    fresh.knowledge.save_passages([{"text": "Chapter one is about habits."}], source={"title": "Atomic Habits", "kind": "book"})
    assert fresh.knowledge.search_text("habits")["hits"]
    assert {"Source Passage", "Ontology Term", "Passage Search Index"} <= {t["name"] for t in fresh.store.terms(fresh.ds("knowledge")["id"])}


def test_store_without_triggers_is_reindexed_once(kb, settings):
    """A store written before the FTS triggers (0.3.x) gets reindexed once, then updates row by row."""
    con = sqlite3.connect(kb.knowledge.path)
    for (trigger,) in con.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'").fetchall():
        con.execute(f"DROP TRIGGER {trigger}")
    con.execute("DELETE FROM passages_fts")
    con.execute("INSERT INTO passages_fts (rowid, passage_id, source_id, heading, text) VALUES (99, 1, 1, 'stale', 'stale row')")
    con.commit()
    con.close()

    fresh = OntologyService(settings)
    assert [h["passage_id"] for h in fresh.knowledge.search_text("에빙하우스", kinds=["passage"])["hits"]] == [1]
    con = sqlite3.connect(fresh.knowledge.path)
    assert con.execute("SELECT COUNT(*) FROM passages_fts WHERE rowid <> passage_id").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM passages_fts").fetchone()[0] == 3
    con.close()


def test_index_follows_each_write(kb):
    k = kb.knowledge
    out = k.save_passages([{"text": "Interleaving mixes problem types.", "terms": ["Interleaving"]}],
                          source={"title": "Make It Stick", "kind": "book", "summary": "Learning science for students."})
    pid = out["inserted"][0]
    assert k.search_text("interleaving", kinds=["passage"])["hits"][0]["passage_id"] == pid
    k.save_passages([{"id": pid, "text": "Mixing problem types beats blocked practice."}])
    assert not k.search_text("interleaving", kinds=["passage"])["hits"]
    assert k.search_text("blocked practice", kinds=["passage"])["hits"][0]["passage_id"] == pid
    assert k.search_text("students", kinds=["source"])["hits"][0]["title"] == "Make It Stick"
    k.save_concept("Interleaving", "Practising different kinds of problems in one session.")
    assert k.search_text("session", kinds=["term"])["hits"][0]["name"] == "Interleaving"
    kb.update_term("Interleaving", name="Interleaved Practice", datasource="knowledge")
    assert k.search_text("interleaved", kinds=["term"])["hits"][0]["name"] == "Interleaved Practice"
    k.save_passages(delete_ids=[pid])
    assert not k.search_text("blocked practice", kinds=["passage"])["hits"]


async def test_passage_tools_over_mcp(settings):
    svc = OntologyService(settings)
    async with Client(build_server(settings, svc)) as client:
        tools = {t.name: t for t in await client.list_tools()}
        assert {"save_passages", "search_text"} <= set(tools)
        assert tools["search_text"].annotations.read_only_hint is True
        saved = await client.call_tool("save_passages", {
            "passages": [{"text": "Zone 2 training builds aerobic base.", "heading": "Zone 2", "terms": ["Zone 2"]}],
            "source": {"title": "Training notes", "kind": "note", "original_text": "# Zone 2\n\nZone 2 training builds aerobic base."},
        })
        assert saved.data["inserted"] == [1] and saved.data["original"] == "originals/Training notes.md"
        found = await client.call_tool("search_text", {"query": "aerobic"})
        assert found.data["hits"][0]["link"] == "[[originals/Training notes#Zone 2]]"
