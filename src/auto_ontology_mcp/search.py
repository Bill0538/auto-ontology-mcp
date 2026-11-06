"""Lexical search over the ontology.

Auto Ontology retrieves candidates with embeddings. This server has no
embedding model, so it scores token overlap (camel/snake-case split,
singularized), trigram similarity for typos and inflected words, and phrase
containment, with weights per field. The host model compensates for what a
lexical index misses by searching again with synonyms or translations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .naming import normalize_text, tokens, trigram_similarity

KINDS = ("term", "attribute", "sql_attribute", "glossary", "table", "example", "source")


@dataclass
class Doc:
    kind: str
    id: str
    title: str
    fields: list[tuple[str, float]]
    payload: dict[str, Any] = field(default_factory=dict)


def _field_score(q_tokens: list[str], q_norm: str, text: str) -> float:
    if not text:
        return 0.0
    f_tokens = tokens(text)
    if not f_tokens:
        return 0.0
    f_set = set(f_tokens)
    total = 0.0
    for q in q_tokens:
        if q in f_set:
            total += 1.0
            continue
        best = 0.0
        for f in f_set:
            if len(q) >= 2 and len(f) >= 2 and not q.isascii() and (f in q or q in f):
                best = max(best, 0.8)
            elif abs(len(f) - len(q)) <= 3:
                sim = trigram_similarity(q, f)
                if sim >= 0.45:
                    best = max(best, sim * 0.8)
        total += best
    coverage = total / len(q_tokens)
    norm_text = normalize_text(text)
    if len(q_norm) >= 3 and q_norm in norm_text:
        coverage = max(coverage, 1.0)
    # Prefer tight matches: "Order" beats "Order Line" for the query "order".
    extra = max(0, len(f_set) - len(set(q_tokens)))
    return coverage * (1.0 / (1.0 + 0.04 * extra))


def score(query: str, doc: Doc) -> float:
    q_tokens = tokens(query) or tokens(query, drop_stopwords=False)
    if not q_tokens:
        return 0.0
    q_norm = normalize_text(query).strip()
    scored = sorted((_field_score(q_tokens, q_norm, text) * w for text, w in doc.fields), reverse=True)
    if not scored:
        return 0.0
    best = scored[0] + (0.15 * scored[1] if len(scored) > 1 else 0.0)
    return round(min(1.0, best), 4)


def build_docs(
    terms: list[dict[str, Any]],
    attributes: list[dict[str, Any]],
    sql_attributes: list[dict[str, Any]],
    glossary: list[dict[str, Any]],
    tables: list[dict[str, Any]],
    examples: list[dict[str, Any]],
    relations: list[dict[str, Any]] | None = None,
    sources: list[dict[str, Any]] | None = None,
) -> list[Doc]:
    docs: list[Doc] = []
    attrs_by_term: dict[str, list[dict[str, Any]]] = {}
    for a in attributes:
        attrs_by_term.setdefault(a["term_id"], []).append(a)
    links: dict[str, list[str]] = {}
    for r in relations or []:
        links.setdefault(r["source_term_id"], []).append(f"{r['relation'].replace('_', ' ')} {r['target_name']}")
    modeled_tables = {t.get("primary_table_id") for t in terms} | {a["table_id"] for a in attributes}

    for t in terms:
        syn = " ".join(t.get("synonyms") or [])
        docs.append(
            Doc(
                "term", t["id"], t["name"],
                [(t["name"], 1.0), (syn, 0.95), (t.get("table_name") or "", 0.6), (t.get("description") or "", 0.45),
                 (" ".join(a["display_name"] for a in attrs_by_term.get(t["id"], [])), 0.3),
                 ((t.get("body") or "")[:20000], 0.35), (" ".join(links.get(t["id"], [])), 0.3)],
                {"status": t["status"], "term_kind": t.get("kind") or "entity", "table": t.get("table_name"),
                 "description": t.get("description")},
            )
        )
    for a in attributes:
        samples = a.get("sample_values")
        sample_text = " ".join(str(v) for v in samples) if isinstance(samples, list) else ""
        docs.append(
            Doc(
                "attribute", a["id"], f"{a['term_name']} › {a['display_name']}",
                [(a["display_name"], 0.9), (a["column_name"], 0.75), (f"{a['term_name']} {a['display_name']}", 0.85),
                 (a.get("description") or "", 0.4), (sample_text, 0.35)],
                {"term": a["term_name"], "column": f"{a['table_name']}.{a['column_name']}", "data_type": a.get("data_type")},
            )
        )
    for s in sql_attributes:
        docs.append(
            Doc(
                "sql_attribute", s["id"], f"{s['term_name']} › {s['name']}",
                [(s["name"], 1.0), (s.get("description") or "", 0.5), (s["expression"], 0.25), (s["term_name"], 0.3)],
                {"term": s["term_name"], "sql_kind": s["kind"], "status": s["status"], "expression": s["expression"]},
            )
        )
    for g in glossary:
        docs.append(Doc("glossary", g["id"], g["name"], [(g["name"], 1.0), (g["definition"], 0.6)], {"definition": g["definition"]}))
    for t in tables:
        if t["id"] in modeled_tables:
            continue
        docs.append(
            Doc("table", t["id"], t["name"], [(t["name"], 0.8), (t.get("description") or "", 0.4)],
                {"schema": t.get("schema_name"), "note": "table has no Term yet"})
        )
    for src in sources or []:
        docs.append(
            Doc("source", str(src["id"]), src["title"],
                [(src["title"], 0.9), (src.get("summary") or "", 0.45), (src.get("uri") or "", 0.3), (src.get("content") or "", 0.3)],
                {"source_kind": src.get("kind"), "uri": src.get("uri")})
        )
    for e in examples:
        docs.append(Doc("example", e["id"], e["question"], [(e["question"], 0.9), (e.get("note") or "", 0.4)], {"sql": e["sql"]}))
    return docs


def search(query: str, docs: list[Doc], kinds: list[str] | None = None, limit: int = 10, min_score: float = 0.2) -> list[dict[str, Any]]:
    wanted = set(kinds or KINDS)
    hits = []
    for doc in docs:
        if doc.kind not in wanted:
            continue
        s = score(query, doc)
        if s >= min_score:
            hits.append({**doc.payload, "kind": doc.kind, "id": doc.id, "name": doc.title, "score": s})
    hits.sort(key=lambda h: (-h["score"], {"term": 0, "sql_attribute": 1, "attribute": 2, "glossary": 3, "example": 4, "source": 5, "table": 6}[h["kind"]]))
    return hits[:limit]
