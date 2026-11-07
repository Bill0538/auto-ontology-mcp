"""Export the ontology as an Obsidian-compatible markdown vault.

The store stays the source of truth; this writes a read-only view of it: one
page per Term (synonyms become Obsidian aliases, relations become [[links]]),
one page per source, an index and a change log, the same entry points an LLM
Wiki keeps. Re-exporting rewrites generated pages and removes generated pages
whose Term is gone; files without the generated marker are never touched.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from .knowledge import SOURCES_TABLE, is_builtin, q
from .vault import wikilink

if TYPE_CHECKING:  # pragma: no cover
    from .service import OntologyService

MARKER = "auto-ontology-mcp"
_UNSAFE = re.compile(r'[\\/:*?"<>|#^\[\]]+')
RECORDS_PER_PAGE = 25


def safe_name(name: str) -> str:
    cleaned = _UNSAFE.sub("-", name).strip(" .-")
    return (cleaned or "untitled")[:120]


def _frontmatter(data: dict[str, Any]) -> str:
    body = yaml.safe_dump({k: v for k, v in data.items() if v not in (None, [], "")}, allow_unicode=True, sort_keys=False)
    return f"---\n{body}---\n"


def _cell(value: Any) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", " ")[:120]


def _record_labels(k, terms: list[dict[str, Any]]) -> dict[str, dict[int, Any]]:
    """{table: {id: label}} so referenced records show by name instead of id."""
    out: dict[str, dict[int, Any]] = {}
    with k._write() as con:
        for t in terms:
            spec = t.get("spec") or {}
            label = spec.get("label")
            if spec and label:
                out[spec["table"]] = {r[0]: r[1] for r in con.execute(f"SELECT id, {q(label)} FROM {q(spec['table'])}")}
    return out


def _is_generated(path: Path) -> bool:
    try:
        head = path.read_text(encoding="utf-8")[:400]
    except (OSError, UnicodeDecodeError):
        return False
    return head.startswith("---") and f"generated_by: {MARKER}" in head


def export_wiki(svc: "OntologyService", ds: dict[str, Any], directory: Path) -> dict[str, Any]:
    directory = Path(directory).expanduser()
    (directory / "sources").mkdir(parents=True, exist_ok=True)
    k = svc.knowledge
    terms = svc.store.terms(ds["id"])
    page_of = {t["id"]: safe_name(t["name"]) for t in terms}
    written: set[Path] = set()
    skipped: list[str] = []

    def write(path: Path, text: str) -> None:
        if path.exists() and not _is_generated(path):
            skipped.append(path.name)  # a page the user wrote; never overwrite it
            return
        path.write_text(text, encoding="utf-8")
        written.add(path.resolve())

    def passage_line(p: dict[str, Any], with_source: bool) -> str:
        where = ", ".join(x for x in (p.get("heading"), p.get("locator")) if x)
        text = " ".join(p["text"].split())
        line = f"- {'> ' if p['kind'] == 'quote' else ''}{text}" + (f" ({p['kind']})" if p["kind"] != "quote" else "")
        if p.get("original"):
            line += " " + wikilink(p["original"], p.get("heading"), label=where or "original")
        elif where:
            line += f" ({where})"
        if with_source and p["source_id"] in source_pages:
            line += f" from [[{source_pages[p['source_id']]}|{p['source_title']}]]"
        if p.get("terms"):
            line += " about " + ", ".join(f"[[{safe_name(n)}]]" for n in p["terms"])
        return line

    # Sources (knowledge store only).
    source_pages: dict[int, str] = {}
    sources: list[dict[str, Any]] = []
    if k.is_knowledge(ds) and k.path.exists():
        with k._write() as con:
            sources = [dict(r) for r in con.execute(f"SELECT * FROM {SOURCES_TABLE} ORDER BY id")]
        for src in sources:
            source_pages[src["id"]] = f"sources/{src['id']} - {safe_name(src['title'])}"

    relations = svc.store.all(
        """
        SELECT r.*, s.name AS from_name, t.name AS to_name FROM term_relation r
        JOIN term s ON s.id = r.source_term_id JOIN term t ON t.id = r.target_term_id
        WHERE r.datasource_id = ?
        """,
        (ds["id"],),
    )

    # Built-in tables (sources, passages, the Term mirror, search indexes) get no Term page;
    # sources are shown as the sources/ folder.
    terms = [t for t in terms if not (k.is_knowledge(ds) and is_builtin(t))]
    labels = _record_labels(k, svc.store.terms(ds["id"])) if k.is_knowledge(ds) and k.path.exists() else {}

    for t in terms:
        view = svc.get_term(t["id"], datasource=ds["name"])
        spec = t.get("spec") or {}
        lines = [
            _frontmatter(
                {
                    "generated_by": MARKER,
                    "datasource": ds["name"],
                    "kind": view.get("kind"),
                    "status": view["status"],
                    "aliases": view.get("synonyms") or [],
                    "table": view.get("table"),
                    "records": (view.get("knowledge") or {}).get("records"),
                }
            ),
            f"# {view['name']}",
            "",
        ]
        if view.get("description"):
            lines += [f"> {view['description']}", ""]
        if view.get("body"):
            lines += [view["body"].strip(), ""]
        outgoing = [r for r in relations if r["source_term_id"] == t["id"]]
        incoming = [r for r in relations if r["target_term_id"] == t["id"]]
        if outgoing or incoming:
            lines.append("## Relations")
            for r in outgoing:
                lines.append(f"- {r['relation'].replace('_', ' ')} [[{page_of[r['target_term_id']]}]]" + (f": {r['description']}" if r.get("description") else ""))
            for r in incoming:
                lines.append(f"- [[{page_of[r['source_term_id']]}]] {r['relation'].replace('_', ' ')} this")
            lines.append("")
        joins = [j for j in view.get("related_terms") or [] if not j["join"].endswith(f"= {SOURCES_TABLE}.id")]
        if joins:
            lines.append("## Joins")
            for j in joins:
                targets = ", ".join(f"[[{safe_name(n)}]]" for n in j["terms"]) or "an unmodeled table"
                lines.append(f"- {targets} via `{j['join']}` ({j['multiplicity']}{', role ' + j['role'] if j.get('role') else ''})")
            lines.append("")
        attrs = view.get("attributes") or []
        if attrs:
            lines += ["## Attributes", "", "| Attribute | Column | Type |", "| --- | --- | --- |"]
            for a in attrs:
                lines.append(f"| {_cell(a['name'])} | `{a['column']}` | {_cell(a.get('type'))} |")
            lines.append("")
        if view.get("sql_attributes"):
            lines.append("## SQL attributes")
            for s in view["sql_attributes"]:
                lines.append(f"- **{s['name']}** ({s['kind']}, {s['status']}): `{s['expression']}`" + (f" {s['description']}" if s.get("description") else ""))
            lines.append("")
        if spec and spec["table"] != SOURCES_TABLE and k.path.exists():
            cols = [c["name"] for c in spec["columns"]]
            with k._write() as con:
                rows = [dict(r) for r in con.execute(
                    f"SELECT id, {', '.join(q(c) for c in cols)}, _source_id FROM {q(spec['table'])} ORDER BY id DESC LIMIT {RECORDS_PER_PAGE}"
                )]
            if rows:
                total = (view.get("knowledge") or {}).get("records", len(rows))
                lines += [f"## Records ({total})", "", "| id | " + " | ".join(cols) + " | source |", "| --- " * (len(cols) + 2) + "|"]
                refs = {c["name"]: c["ref_table"] for c in spec["columns"] if c["type"] == "ref"}
                titles = {src["id"]: src["title"] for src in sources}
                for r in rows:
                    src = r.get("_source_id")
                    link = f"[[{source_pages[src]}\\|{_cell(titles[src])}]]" if src in source_pages else ""
                    cells = [_cell(labels.get(refs[c], {}).get(r[c], r[c])) if c in refs else _cell(r[c]) for c in cols]
                    lines.append(f"| {r['id']} | " + " | ".join(cells) + f" | {link} |")
                if total > len(rows):
                    lines.append(f"\nShowing the latest {len(rows)}. Query the rest with run_sql.")
                lines.append("")
        passages = k.index.passages_for(term_id=t["id"]) if k.is_knowledge(ds) else []
        if passages:
            lines += [f"## Passages ({len(passages)})", ""] + [passage_line(p, with_source=True) for p in passages] + [""]
        if view.get("sources"):
            lines.append("## Sources")
            for src in view["sources"]:
                page = source_pages.get(src.get("id"))
                label = f"[[{page}|{src.get('title', src.get('id'))}]]" if page else src.get("title", "")
                lines.append(f"- {label}" + (f", {src['locator']}" if src.get("locator") else "") + (f": \"{src['evidence']}\"" if src.get("evidence") else ""))
            lines.append("")
        write(directory / f"{page_of[t['id']]}.md", "\n".join(lines))

    for src in sources:
        detail = k.get_source(src["id"])
        lines = [
            _frontmatter({"generated_by": MARKER, "kind": src.get("kind"), "uri": src.get("uri"), "author": src.get("author"),
                          "published_on": src.get("published_on"), "captured_at": src.get("captured_at"),
                          "original": src.get("original")}),
            f"# {src['title']}",
            "",
        ]
        if src.get("original"):
            lines += [f"Original: {wikilink(src['original'])}", ""]
        if src.get("summary"):
            lines += [src["summary"], ""]
        if detail["records"] or detail["concepts"]:
            lines.append("## Stored from this source")
            for r in detail["records"]:
                lines.append(f"- {r['records']} [[{safe_name(r['entity'])}]] record(s)")
            for c in detail["concepts"]:
                lines.append(f"- [[{safe_name(c['name'])}]]" + (f", {c['locator']}" if c.get("locator") else ""))
            lines.append("")
        if detail.get("passages"):
            lines += [f"## Passages ({len(detail['passages'])})", ""] + [passage_line(p, with_source=False) for p in detail["passages"]] + [""]
        if src.get("content"):
            lines += ["## Content", "", src["content"], ""]
        write(directory / f"{source_pages[src['id']]}.md", "\n".join(lines))

    entities = [t for t in terms if (t.get("kind") or "entity") == "entity"]
    concepts = [t for t in terms if t.get("kind") == "concept"]
    index = [_frontmatter({"generated_by": MARKER, "datasource": ds["name"]}), f"# {ds['name']} index", ""]
    for title, group in (("Entities", entities), ("Concepts", concepts)):
        if not group:
            continue
        index += [f"## {title}", ""]
        for t in group:
            desc = (t.get("description") or "").strip() or ("empty, to fill in" if title == "Concepts" and not t.get("body") else "")
            index.append(f"- [[{page_of[t['id']]}]]" + (f": {desc}" if desc else "") + (" ✓" if t["status"] == "certified" else ""))
        index.append("")
    if sources:
        index += [f"## Sources ({len(sources)})", ""] + [f"- [[{source_pages[s['id']]}|{s['title']}]]" for s in sources] + [""]
    write(directory / "index.md", "\n".join(index))

    log = [_frontmatter({"generated_by": MARKER}), "# Log", ""]
    for c in svc.recent_changes(limit=200)["changes"]:
        log.append(f"- {c['at']} {c['what']}")
    write(directory / "log.md", "\n".join(log) + "\n")

    removed = []
    for path in list(directory.glob("*.md")) + list((directory / "sources").glob("*.md")):
        if path.resolve() not in written and _is_generated(path):
            path.unlink()
            removed.append(path.name)
    return {
        "directory": str(directory),
        "pages": len(written),
        "removed_stale_pages": removed,
        "skipped_user_pages": skipped,
        "originals": str(k.originals_dir) if k.is_knowledge(ds) else None,
        "next_step": "Open the vault in Obsidian. The store stays the source of truth for generated pages: edits made "
        "to them in Obsidian are not read back, so change knowledge through the assistant and export again. Originals "
        "are ordinary notes and files: edit them freely.",
    }

