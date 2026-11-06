"""The MCP surface: tools, prompts and a guide resource."""

from __future__ import annotations

import functools
import logging
from typing import Annotated, Any, Callable, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import __version__, guidance
from .config import Settings
from .service import OntologyError, OntologyService

logger = logging.getLogger(__name__)

READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)
READ_DB = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=True)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
WRITE_DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False)
CONNECT = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=True)

DatasourceArg = Annotated[
    str | None,
    Field(description="Datasource name. Optional when only one datasource is registered."),
]


class ColumnAssignment(BaseModel):
    column: str = Field(description="Physical column name, exactly as listed in candidate_columns.")
    display_name: str = Field(description="User-friendly label with spaces, e.g. 'Order Date'.")
    description: str | None = Field(default=None, description="Optional one-sentence business description.")


class TermInput(BaseModel):
    name: str = Field(description="Business Term name with spaces, e.g. 'Purchase Order'.")
    description: str = Field(description="Business meaning: what one row is and which population it covers.")
    synonyms: list[str] = Field(default_factory=list, description="Alternate names actually used (abbreviations, other languages).")
    columns: list[ColumnAssignment] = Field(description="Every candidate column of the table, each assigned to exactly one Term.")


class ColumnUpdate(BaseModel):
    column: str = Field(description="Physical column name mapped by this Term.")
    display_name: str | None = None
    description: str | None = None


class EntityColumn(BaseModel):
    name: str = Field(description="snake_case English column name, e.g. 'paid_on', 'total_krw'. 'id' and '_*' are reserved.")
    type: str = Field(default="text", description="text, integer, number, date, datetime, boolean, or ref:<Entity> (e.g. 'ref:Merchant').")
    display_name: str | None = Field(default=None, description="Label in the user's words, e.g. 'Paid On'.")
    description: str | None = Field(default=None, description="Meaning, unit or format.")
    required: bool = Field(default=False, description="Reject records without this value.")


class SourceInput(BaseModel):
    id: int | None = Field(default=None, description="Existing source id; omit to register a new source.")
    title: str | None = Field(default=None, description="File name, page title or a short label, e.g. 'IMG_2041.jpg'.")
    kind: str | None = Field(default=None, description="pdf, image, web, note, conversation, email, book, ...")
    uri: str | None = Field(default=None, description="Path or URL of the original, when known.")
    author: str | None = None
    published_on: str | None = Field(default=None, description="YYYY-MM-DD")
    summary: str | None = Field(default=None, description="What the source is about, in a sentence or two.")
    content: str | None = Field(default=None, description="Transcribed text or the relevant excerpt.")


class RelationInput(BaseModel):
    relation: str = Field(description="snake_case verb: is_a, part_of, has_part, instance_of, related_to, depends_on, causes, used_for, contrasts_with, same_as, example_of, ...")
    target: str = Field(description="Target Term name; created as an empty concept if it does not exist yet.")
    description: str | None = None
    evidence: str | None = Field(default=None, description="Quote that supports the link.")


class RelationRef(BaseModel):
    relation: str
    target: str


SourceArg = Annotated[
    int | SourceInput | None,
    Field(description="Where this came from: an existing source id, or a new source to register (deduplicated by uri or title)."),
]

DEFINE_ENTITY_DESCRIPTION = 'Create an entity in the personal knowledge store (a Term with its own table of records), add columns to it as you learn more, or delete it. Search first (search_ontology or list_terms) and reuse an existing entity. Use entities for facts you will count, sum, filter or compare; use save_concept for definitions and explanations. Column types: text, integer, number, date, datetime, boolean, ref:<Entity> (a link that becomes a relationship). Names are snake_case English (pass table= when the entity name is not English); keys make re-adding a record update it. Existing column types cannot change. delete=true drops the entity; with force=true its records are deleted too, which cannot be undone. Then store rows with add_records.'


class GlossaryEntry(BaseModel):
    name: str = Field(description="Abbreviation or jargon, e.g. 'MRR'.")
    definition: str = Field(description="What it means, e.g. 'monthly recurring revenue'.")


def _tool_errors(fn: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except OntologyError as exc:
            raise ToolError(str(exc)) from exc

    return wrapper


def build_server(settings: Settings | None = None, service: OntologyService | None = None) -> FastMCP:
    settings = settings or Settings.from_env()
    svc = service or OntologyService(settings)
    mcp = FastMCP(
        name="auto-ontology",
        instructions=guidance.SERVER_INSTRUCTIONS,
        version=__version__,
        website_url="https://github.com/gogoymh/auto-ontology-mcp",
    )

    def tool(annotations: ToolAnnotations, **kw: Any) -> Callable[[Callable[..., Any]], Any]:
        def deco(fn: Callable[..., Any]) -> Any:
            return mcp.tool(annotations=annotations, **kw)(_tool_errors(fn))

        return deco

    # ------------------------------------------------------------- readiness
    @tool(READ_DB)
    def check_readiness() -> dict[str, Any]:
        """Report every datasource's state: whether SQL can run against it, how much is modeled (Terms,
        relationships, SQL attributes), knowledge store counts, and the next step. Call it first in
        a session and whenever a question fails unexpectedly. Read-only."""
        return svc.check_readiness()

    # ---------------------------------------------------------- datasources
    if settings.allow_datasource_tools:

        @tool(CONNECT)
        def add_datasource(
            name: Annotated[str, Field(description="Short name, e.g. 'shop'.")],
            url: Annotated[
                str,
                Field(
                    description="SQLAlchemy URL, e.g. sqlite:////abs/path/file.db, "
                    "postgresql+psycopg://user:pass@host:5432/db, mysql+pymysql://user:pass@host/db, duckdb:///file.duckdb."
                ),
            ],
            description: Annotated[str | None, Field(description="What this database holds.")] = None,
            schemas: Annotated[list[str] | None, Field(description="Schemas to include (default: all non-system schemas).")] = None,
            scan: Annotated[bool, Field(description="Scan the schema right away.")] = True,
        ) -> dict[str, Any]:
            """Register a database by SQLAlchemy URL, test the connection and, by default, scan it. Use it only
            when the user gives you a URL; never invent credentials. Connections are opened
            read-only and nothing is ever written to the database. Re-using a name replaces that
            datasource's URL. Fails with the driver or connection error if the database cannot
            be reached; use scan_datasource to re-read one that is already registered."""
            return svc.add_datasource(name, url, description, schemas, scan)

        @tool(WRITE)
        def scan_datasource(
            datasource: DatasourceArg = None,
            schemas: Annotated[list[str] | None, Field(description="Limit the scan to these schemas.")] = None,
            profile: Annotated[bool, Field(description="Sample rows to profile columns and test inferred joins.")] = True,
        ) -> dict[str, Any]:
            """Re-read a registered database: tables, columns, declared keys, column profiles (samples,
            uniqueness, formats, small value sets) and undeclared keys inferred by name and
            confirmed by value containment. Use it after the schema changed or to refresh
            profiles; add_datasource already scans once. Terms, reviewed relationships and SQL
            attributes are kept; inferred relationships that lost their evidence are removed.
            Reads the database only."""
            return svc.scan_datasource(datasource, schemas, profile)

    # ------------------------------------------------------------- modeling
    @tool(READ)
    def get_modeling_worklist(
        datasource: DatasourceArg = None,
        limit: Annotated[int, Field(description="Tables to return (1-10).")] = 3,
        tables: Annotated[list[str] | None, Field(description="Specific tables to (re)model instead of the next pending ones.")] = None,
    ) -> dict[str, Any]:
        """Return the next tables that still have no business Terms, with what you need to name them: candidate
        columns (types, samples, uniqueness, formats), foreign keys and the Terms they point to, and
        the modeling rules. Use it while building an ontology over a database; call save_table_terms
        once per returned table and repeat until remaining is 0. For the personal knowledge store
        use define_entity instead. Read-only."""
        return svc.get_modeling_worklist(datasource, limit, tables)

    @tool(WRITE)
    def save_table_terms(
        table: Annotated[str, Field(description="Table name, schema-qualified if needed (e.g. 'sales.orders').")],
        terms: Annotated[list[TermInput], Field(description="Usually exactly one Term per table.")],
        column_descriptions: Annotated[
            dict[str, str] | None, Field(description="Optional {column: one-sentence description} for non-obvious columns.")
        ] = None,
        is_junction: Annotated[
            bool | None, Field(description="True if each row only associates two or more entities (bridge table).")
        ] = None,
        datasource: DatasourceArg = None,
    ) -> dict[str, Any]:
        """Save the Terms you propose for one database table and map its columns to them. Use it for tables
        returned by get_modeling_worklist; use update_term to edit a single Term afterwards. It
        replaces the table's previous Terms: same-named Terms are updated, others are removed unless
        they own SQL attributes. Unassigned candidate columns go to the first Term. Nothing is saved
        if a column is unknown, assigned twice, or a name clashes with another table's Term; the
        error lists every problem. New Terms are drafts."""
        return svc.save_table_terms(table, [t.model_dump() for t in terms], column_descriptions, is_junction, datasource)

    @tool(READ)
    def list_relationships(
        status: Annotated[Literal["proposed", "accepted", "rejected", "all"], Field(description="Which relationships to list.")] = "proposed",
        table: Annotated[str | None, Field(description="Only relationships touching this table.")] = None,
        datasource: DatasourceArg = None,
    ) -> dict[str, Any]:
        """List joins between tables with their evidence (declared, inferred with name score and value
        containment, or added manually), plus multi-role joins, identifier columns with no
        relationship, and joins used by executed queries that are not relationships yet. Use it to
        review the join graph before modeling or when a join looks wrong; act on it with
        set_relationship. Use find_join_path for the route between two Terms. Read-only."""
        return svc.list_relationships(datasource, status, table)

    @tool(WRITE_DESTRUCTIVE)
    def set_relationship(
        action: Annotated[Literal["accept", "reject", "add", "update", "delete"], Field(description="What to do.")],
        relationship_id: Annotated[str | None, Field(description="Id from list_relationships (or give source and target).")] = None,
        source: Annotated[str | None, Field(description="Foreign-key side, 'table.column'.")] = None,
        target: Annotated[str | None, Field(description="Referenced key, 'table.column'.")] = None,
        role: Annotated[str | None, Field(description="Business role when several columns reference the same table, e.g. 'billing'.")] = None,
        description: Annotated[str | None, Field(description="What the relationship means.")] = None,
        is_default: Annotated[bool | None, Field(description="Make this the default join between the two tables.")] = None,
        datasource: DatasourceArg = None,
    ) -> dict[str, Any]:
        """Change one join between two columns: accept or reject a proposed one, add a missing one, update its
        role, description or default flag, or delete it. Use it after list_relationships. Identify
        it by relationship_id or by source and target ('table.column'). Reviewed relationships
        survive re-scans; only accepted ones are used for join paths. delete removes it permanently;
        rejecting keeps the decision instead. Fails if the columns or relationship do not exist."""
        return svc.set_relationship(action, relationship_id, source, target, role, description, is_default, datasource)

    @tool(READ)
    def suggest_sql_attributes(
        term: Annotated[str | None, Field(description="Limit to one Term.")] = None,
        limit: Annotated[int, Field(description="Maximum candidates.")] = 15,
        datasource: DatasourceArg = None,
    ) -> dict[str, Any]:
        """Return candidate SQL attributes (reusable measures, filters and join patterns) mined from queries
        run through this server, verified examples, bridge tables and column profiles, with the
        rules for judging them. Use it after Terms exist; save the worthwhile ones with
        save_sql_attribute and ignore the rest. Read-only."""
        return svc.suggest_sql_attributes(datasource, term, limit)

    @tool(WRITE_DESTRUCTIVE)
    def save_sql_attribute(
        term: Annotated[str, Field(description="Owning Term name.")],
        name: Annotated[str, Field(description="Title Case name, e.g. 'Net Revenue'.")],
        expression: Annotated[
            str | None,
            Field(
                description="SQL in the datasource dialect. measure/dimension/filter: a fragment over the Term's own "
                "table qualified by the bare table name (SUM(order_lines.line_total)). join/query: a full SELECT."
            ),
        ] = None,
        description: Annotated[str | None, Field(description="What it captures and when to use it.")] = None,
        kind: Annotated[Literal["measure", "filter", "dimension", "join", "query"], Field(description="Type of expression.")] = "measure",
        status: Annotated[Literal["draft", "certified", "deprecated"] | None, Field(description="Set certified only when a person approved it.")] = None,
        delete: Annotated[bool, Field(description="Delete this SQL attribute.")] = False,
        datasource: DatasourceArg = None,
    ) -> dict[str, Any]:
        """Create, update, certify or delete a named SQL expression under a Term. Before saving, the expression
        is run read-only against the database (a fragment over the Term's table, or a full SELECT
        for join/query kinds); if the database rejects it, nothing is saved and the error is
        returned. Changing a certified attribute's expression returns it to draft. delete=true
        removes it permanently. Use suggest_sql_attributes to find candidates."""
        return svc.save_sql_attribute(term, name, expression, description, kind, status, delete, datasource)

    @tool(WRITE_DESTRUCTIVE)
    def update_term(
        term: Annotated[str, Field(description="Term name, synonym or id.")],
        name: Annotated[str | None, Field(description="New name.")] = None,
        description: Annotated[str | None, Field(description="New description.")] = None,
        synonyms: Annotated[list[str] | None, Field(description="Replace all synonyms.")] = None,
        add_synonyms: Annotated[list[str] | None, Field(description="Add synonyms, e.g. the user's own words or translations.")] = None,
        status: Annotated[
            Literal["draft", "certified", "deprecated"] | None,
            Field(description="certified = a person confirmed this definition. Do not certify on your own."),
        ] = None,
        note: Annotated[str | None, Field(description="Reviewer note.")] = None,
        column_updates: Annotated[list[ColumnUpdate] | None, Field(description="Rename or describe mapped columns.")] = None,
        delete: Annotated[bool, Field(description="Delete the Term with its column mappings and SQL attributes.")] = False,
        datasource: DatasourceArg = None,
    ) -> dict[str, Any]:
        """Edit one existing Term: rename it, change its description or synonyms, rename or describe its mapped
        columns, set its status, or delete it. Use add_synonyms to record the user's own words so
        searches match them. Changing a certified Term's name or description returns it to draft
        unless you also pass status; only certify what the user confirmed. delete=true removes the
        Term with its column mappings, SQL attributes and relations and cannot be undone; knowledge
        entities must be deleted with define_entity. For concept bodies and relations use
        save_concept."""
        updates = [c.model_dump() for c in column_updates] if column_updates else None
        return svc.update_term(term, name, description, synonyms, add_synonyms, status, note, updates, delete, datasource)

    @tool(WRITE)
    def save_glossary(
        entries: Annotated[list[GlossaryEntry] | None, Field(description="Entries to add or update.")] = None,
        delete: Annotated[list[str] | None, Field(description="Entry names to delete.")] = None,
        global_scope: Annotated[bool, Field(description="Store for all datasources instead of one.")] = False,
        datasource: DatasourceArg = None,
    ) -> dict[str, Any]:
        """Add, update or delete glossary entries for abbreviations and jargon (MRR → monthly recurring
        revenue), per datasource or global. Entries are searched like Terms and returned by
        get_query_context. Use it for words to expand, not for business definitions with columns,
        which are Terms. Deleted entries are gone."""
        payload = [e.model_dump() for e in entries] if entries else None
        return svc.save_glossary(payload, delete, datasource, global_scope)

    # ------------------------------------------------------- knowledge store
    @tool(WRITE_DESTRUCTIVE, title="Define or change an entity", description=DEFINE_ENTITY_DESCRIPTION)
    def define_entity(
        name: Annotated[str, Field(description="Entity (Term) name, singular, e.g. 'Receipt', 'Book', '운동 기록'.")],
        columns: Annotated[list[EntityColumn] | None, Field(description="Columns to create or add. Existing columns keep their type.")] = None,
        description: Annotated[str | None, Field(description="What one record is.")] = None,
        table: Annotated[str | None, Field(description="snake_case table name; required when the name is not English.")] = None,
        keys: Annotated[list[str] | None, Field(description="Columns that identify a record (e.g. ['isbn']); re-adding updates it.")] = None,
        synonyms: Annotated[list[str] | None, Field(description="Other names, e.g. the user's language.")] = None,
        delete: Annotated[bool, Field(description="Delete the entity (and, with force, its records).")] = False,
        force: Annotated[bool, Field(description="Allow deleting an entity that still has records.")] = False,
    ) -> dict[str, Any]:
        cols = [c.model_dump() for c in columns] if columns else None
        return svc.knowledge.define_entity(name, cols, description, table, keys, synonyms, delete, force)

    @tool(WRITE, title="Add knowledge records")
    def add_records(
        entity: Annotated[str, Field(description="Entity name, e.g. 'Receipt'.")],
        records: Annotated[
            list[dict[str, Any]],
            Field(
                description="Objects of column values. Strings like '12,000원' or '2026/10/03' are normalized. Ref columns accept "
                "an id, a name (created when missing) or an object. Optional per record: id (update that row), _locator, "
                "_evidence, _note, _source."
            ),
        ],
        source: SourceArg = None,
        on_conflict: Annotated[Literal["update", "skip", "error"], Field(description="When a record matches an existing one by keys (or is identical).")] = "update",
        create_missing_refs: Annotated[bool, Field(description="Create referenced records (a new merchant, author) by name.")] = True,
    ) -> dict[str, Any]:
        """Store records in a knowledge entity (from receipts, statements, notes, logs the user shared), each
        with its source, locator and supporting quote. The whole batch is validated first: values
        are normalized ('12,000원' → 12000, '2026/10/03' → 2026-10-03), references resolved by id or
        name (missing ones created), and if any record is invalid nothing is written and every
        problem is listed. Records matching an existing one by key, or identical ones, update or are
        skipped instead of duplicating. Use edit_records to change records by id."""
        src = source.model_dump(exclude_none=True) if isinstance(source, SourceInput) else source
        return svc.knowledge.add_records(entity, records, src, on_conflict, create_missing_refs)

    @tool(WRITE_DESTRUCTIVE, title="Update or delete knowledge records")
    def edit_records(
        entity: Annotated[str, Field(description="Entity name.")],
        action: Annotated[Literal["update", "delete"], Field(description="What to do with the records.")],
        ids: Annotated[list[int], Field(description="Record ids (find them with run_sql or get_source).")],
        values: Annotated[dict[str, Any] | None, Field(description="For update: column values to set; may include _locator, _evidence, _note.")] = None,
        source: SourceArg = None,
    ) -> dict[str, Any]:
        """Update or delete knowledge records by id (find ids with run_sql or get_source). update sets the
        given columns and provenance fields. delete is permanent and is refused while other records
        reference the rows; confirm with the user first. Use add_records to insert new records and
        define_entity to change columns."""
        src = source.model_dump(exclude_none=True) if isinstance(source, SourceInput) else source
        return svc.knowledge.edit_records(entity, action, ids, values, src)

    @tool(WRITE, title="Save a concept")
    def save_concept(
        name: Annotated[str, Field(description="Concept (Term) name, e.g. 'Spaced Repetition'. Any language.")],
        description: Annotated[str | None, Field(description="One-sentence definition.")] = None,
        body: Annotated[str | None, Field(description="Markdown page that replaces the current body: what it is, how it works, examples, open questions.")] = None,
        append_body: Annotated[str | None, Field(description="Markdown appended to the current body (new notes, a new source's view).")] = None,
        synonyms: Annotated[list[str] | None, Field(description="Other names to add.")] = None,
        relations: Annotated[list[RelationInput] | None, Field(description="Typed links to other Terms.")] = None,
        remove_relations: Annotated[list[RelationRef] | None, Field(description="Links to remove.")] = None,
        source: SourceArg = None,
        locator: Annotated[str | None, Field(description="Where in the source (page, chapter, timestamp).")] = None,
        evidence: Annotated[str | None, Field(description="Supporting quote.")] = None,
        status: Annotated[Literal["draft", "certified", "deprecated"] | None, Field(description="Certify only when the user confirms.")] = None,
        datasource: Annotated[str | None, Field(description="Defaults to the knowledge store; pass another datasource to document its Terms.")] = None,
    ) -> dict[str, Any]:
        """Create or update a concept: a Term with a one-line definition, a markdown body (a wiki page) and
        typed relations (is_a, part_of, related_to, ...) to other Terms, with its source. Use it for
        ideas and explanations; use define_entity and add_records for countable facts. Also works on
        existing entity Terms to explain them. body replaces the page and append_body adds to it;
        relation targets that do not exist yet are created as empty concepts. Changing a certified
        concept's meaning returns it to draft."""
        src = source.model_dump(exclude_none=True) if isinstance(source, SourceInput) else source
        rels = [r.model_dump() for r in relations] if relations else None
        removes = [r.model_dump() for r in remove_relations] if remove_relations else None
        return svc.knowledge.save_concept(name, description, body, append_body, synonyms, rels, removes, src, locator, evidence, status, datasource)

    @tool(READ, title="Show a source")
    def get_source(
        source: Annotated[str, Field(description="Source id (e.g. '3' or 'source:3') or part of its title.")],
    ) -> dict[str, Any]:
        """Show one source (by id or part of its title) with its summary and content, and everything stored
        from it: records per entity, concepts and relations. Use it to cite where an answer came
        from or to review what a document contributed; use search_ontology to find sources by topic.
        Read-only."""
        return svc.knowledge.get_source(source)

    @tool(READ, title="Index of Terms")
    def list_terms(
        kind: Annotated[Literal["all", "entity", "concept"], Field(description="Which Terms to list.")] = "all",
        status: Annotated[Literal["draft", "certified", "deprecated"] | None, Field(description="Only Terms with this status.")] = None,
        limit: Annotated[int, Field(description="Page size (max 500).")] = 200,
        offset: Annotated[int, Field(description="Page offset.")] = 0,
        datasource: DatasourceArg = None,
    ) -> dict[str, Any]:
        """List every Term of a datasource (the index): entities and concepts with their one-line definition,
        status, synonyms, record and relation counts, and which concepts are still empty. Use it to
        browse or before adding knowledge, to reuse Terms instead of creating near-duplicates; use
        search_ontology to find Terms by words and get_term for one Term's full detail. Read-only."""
        return svc.list_terms(datasource, kind, status, limit, offset)

    @tool(READ, title="Recent changes")
    def recent_changes(limit: Annotated[int, Field(description="How many entries, newest first.")] = 30) -> dict[str, Any]:
        """List recent changes across the ontology and knowledge store (the log), newest first: Terms, records,
        concepts, relationships and SQL attributes added, changed, certified or deleted. Use it to
        answer 'what did I add lately' or to review a session's work. Read-only."""
        return svc.recent_changes(limit)

    @tool(WRITE, title="Export as an Obsidian vault")
    def export_wiki(datasource: DatasourceArg = None) -> dict[str, Any]:
        """Write the ontology (by default the knowledge store) as an Obsidian vault in
        ~/.auto-ontology-mcp/wiki/<datasource>: one page per Term with synonyms as aliases and
        relations as [[links]], one per source, plus index.md and log.md. Use it when the user wants
        to browse their knowledge visually. Re-exporting rewrites and prunes only pages it
        generated; other files in the folder are left alone. Edits made in Obsidian are not read
        back. Use export_ontology for a single YAML/JSON document."""
        return svc.export_wiki(datasource)

    # -------------------------------------------------------------- reading
    @tool(READ)
    def search_ontology(
        query: Annotated[str, Field(description="Words to look for: a business noun, metric, column or value.")],
        kinds: Annotated[
            list[Literal["term", "attribute", "sql_attribute", "glossary", "table", "example", "source", "value"]] | None,
            Field(description="Restrict result kinds."),
        ] = None,
        limit: Annotated[int, Field(description="Maximum hits.")] = 10,
        datasource: DatasourceArg = None,
    ) -> dict[str, Any]:
        """Find Terms, concepts (including their bodies and relations), mapped columns, SQL attributes,
        glossary entries, verified example questions, sources and unmodeled tables by words. In the
        knowledge store it also matches records by their stored text ('value' hits). Use it to
        locate things by name or topic; use list_terms to browse everything and get_term for full
        detail. Matching is lexical, not semantic: use the schema's vocabulary, try synonyms and
        translate. Read-only."""
        return svc.search_ontology(query, list(kinds) if kinds else None, limit, datasource)

    @tool(READ)
    def get_term(
        term: Annotated[str, Field(description="Term name, synonym or id.")],
        datasource: DatasourceArg = None,
    ) -> dict[str, Any]:
        """Return one Term in full: definition, status, synonyms, body, mapped columns with sample values and
        formats, SQL attributes, related Terms with join conditions, typed relations and sources,
        plus record counts for knowledge entities. Accepts a name, synonym or id; if nothing
        matches, the error lists the closest Terms. Use it to answer 'what is X' and before writing
        SQL about X; use search_ontology when you do not know the name and describe_table for a
        physical table. Read-only."""
        return svc.get_term(term, datasource)

    @tool(READ)
    def describe_table(
        table: Annotated[str, Field(description="Table name, schema-qualified if needed.")],
        datasource: DatasourceArg = None,
    ) -> dict[str, Any]:
        """Return one physical table: every column (including ones no Term maps), keys, junction status, the
        Terms and attributes on it, its relationships and SQL attributes. Use it to inspect a
        table's structure; use get_term for business meaning and get_source for
        provenance. Fails with suggestions if the table name is unknown. Read-only."""
        return svc.describe_table(table, datasource)

    @tool(READ)
    def find_join_path(
        source: Annotated[str, Field(description="Term or table to start from.")],
        target: Annotated[str, Field(description="Term or table to reach.")],
        datasource: DatasourceArg = None,
    ) -> dict[str, Any]:
        """Return the shortest chain of accepted relationships between two Terms or tables, with ON conditions,
        a ready FROM/JOIN clause, fan-out warnings and other roles for the same hop (billing vs
        delivery city). Use it when you need one specific route; get_query_context already includes
        the path for a set of Terms. Reports found=false when no chain exists. Read-only."""
        return svc.find_join_path(source, target, datasource)

    # ------------------------------------------------------------ answering
    @tool(READ)
    def check_answerable(
        question: Annotated[str, Field(description="The user's question.")],
        entities: Annotated[
            list[str] | None,
            Field(description="1-5 entity phrases you extracted from the question, in the ontology's language. " + guidance.ENTITY_RULES),
        ] = None,
        datasource: DatasourceArg = None,
    ) -> dict[str, Any]:
        """Check, before any SQL, whether the ontology covers each entity of the question. Extract 1-5 entity
        phrases yourself and pass them; each comes back covered (by a Term, attribute or SQL
        attribute), covered_by_value (a name stored in the data), weak, or not covered, with an
        overall verdict and the next step. When something material is not covered, tell the user
        instead of substituting a similar measure. Read-only and fast; use get_query_context next."""
        return svc.check_answerable(question, entities, datasource)

    @tool(READ)
    def get_query_context(
        terms: Annotated[list[str], Field(description="Terms you will use (names, synonyms or ids).")],
        question: Annotated[str | None, Field(description="The question, to find verified example queries.")] = None,
        datasource: DatasourceArg = None,
    ) -> dict[str, Any]:
        """Return what you need to write the SQL for a set of Terms: qualified tables, mapped columns with
        samples and formats, SQL attributes, the join path connecting them (with a ready FROM/JOIN
        clause and role notes), verified example queries, glossary and the dialect's SQL rules. Call
        it after check_answerable with the Terms you will use. Unknown Terms are listed under
        unresolved with suggestions; if none resolve, it fails. Read-only."""
        return svc.get_query_context(terms, question, datasource)

    @tool(READ_DB)
    def lookup_values(
        column: Annotated[str, Field(description="'table.column'.")],
        contains: Annotated[str | None, Field(description="Case-insensitive text to look for. Omit to list the most common values.")] = None,
        limit: Annotated[int, Field(description="Maximum values.")] = 20,
        datasource: DatasourceArg = None,
    ) -> dict[str, Any]:
        """Return real values stored in a column ('table.column'): matches for a case-insensitive text, close
        spellings when nothing matches, or the most common values with their counts. Use it to
        ground literal filters (a place, product or status) before writing them into SQL. Read-only."""
        return svc.lookup_values(column, contains, limit, datasource)

    @tool(READ_DB)
    def validate_sql(
        sql: Annotated[str, Field(description="One read-only SELECT in the datasource dialect.")],
        datasource: DatasourceArg = None,
    ) -> dict[str, Any]:
        """Check one query without returning rows: read-only guard, unknown tables or columns (with the
        ontology's name for them), joins the ontology does not know or that use a non-default role,
        near-miss literals, percent-vs-fraction thresholds, one-to-many fan-out under SUM/AVG/COUNT,
        and a dry run; also which Terms it relies on and whether they are certified. Use it before
        run_sql and fix every error. Read-only."""
        return svc.validate_sql(sql, datasource)

    @tool(READ_DB)
    def run_sql(
        sql: Annotated[str, Field(description="One read-only SELECT in the datasource dialect.")],
        question: Annotated[str | None, Field(description="The user's question, kept with the query for provenance.")] = None,
        max_rows: Annotated[int, Field(description="Rows to return (capped by the server).")] = 100,
        datasource: DatasourceArg = None,
    ) -> dict[str, Any]:
        """Execute one read-only SELECT and return columns, rows, row_count and truncated, plus the Terms it
        used and warnings. Use it after validate_sql. Writes, DDL, multiple statements and tables
        outside the catalog are refused, and the connection itself is read-only. The query is logged
        with the question for provenance. Show the SQL with the answer and say when rows are
        truncated."""
        return svc.run_sql(sql, question, max_rows, datasource)

    @tool(WRITE)
    def save_example_query(
        question: Annotated[str, Field(description="The question in the user's words.")],
        sql: Annotated[str, Field(description="The SQL that answered it correctly.")],
        note: Annotated[str | None, Field(description="Why this SQL is right (definitions, exclusions).")] = None,
        datasource: DatasourceArg = None,
    ) -> dict[str, Any]:
        """Store a question and SQL pair the user confirmed as correct. Later, get_query_context offers it for
        similar questions and suggest_sql_attributes mines it. Saving the same question again
        replaces its SQL. The SQL must validate, or nothing is saved."""
        return svc.save_example_query(question, sql, note, datasource)

    # --------------------------------------------------------- import/export
    @tool(READ)
    def export_ontology(
        format: Annotated[Literal["yaml", "json"], Field(description="Document format.")] = "yaml",
        datasource: DatasourceArg = None,
    ) -> dict[str, Any]:
        """Return a datasource's whole semantic layer as one YAML or JSON document: tables, relationships,
        Terms with column mappings, SQL attributes, concepts and typed relations, glossary and
        example queries. Use it for review, backup or moving the ontology; records in the knowledge
        store are not included. Use export_wiki for an Obsidian view. Read-only."""
        return svc.export_ontology(datasource, format)

    @tool(WRITE)
    def import_ontology(
        document: Annotated[str, Field(description="A document produced by export_ontology.")],
        datasource: DatasourceArg = None,
    ) -> dict[str, Any]:
        """Apply a document produced by export_ontology to a datasource that has been scanned, matching tables
        and columns by name. Existing items with the same names are updated; a table's Terms are
        replaced as in save_table_terms. Items that fail (missing tables or columns, invalid SQL)
        are skipped and listed while the rest is applied, so the result can be partial. Knowledge
        entities are recreated empty when their tables are missing; records are not imported."""
        return svc.import_ontology(document, datasource)

    # ------------------------------------------- ChatGPT connectors / research
    @tool(READ)
    def search(query: Annotated[str, Field(description="Search query.")]) -> dict[str, Any]:
        """Search Terms, tables and sources across all datasources and return ids for fetch. Intended for
        ChatGPT connectors and deep research; other clients should prefer search_ontology.
        Read-only."""
        return {"results": svc.search_documents(query)}

    @tool(READ)
    def fetch(id: Annotated[str, Field(description="Document id returned by search, e.g. 'term:term_ab12'.")]) -> dict[str, Any]:
        """Return the full text of one document returned by search: a Term (definition, body, attributes, SQL
        attributes, relations, sources), a table, or a source. Read-only."""
        return svc.fetch_document(id)

    # --------------------------------------------------------------- prompts
    @mcp.prompt(name="build_ontology", description="Walk through modeling a datasource end to end.")
    def build_ontology_prompt(datasource: str = "") -> str:
        target = f" for datasource '{datasource}'" if datasource else ""
        return (
            f"Build the semantic layer{target} with the auto-ontology tools.\n"
            "1. check_readiness. If not scanned, scan_datasource.\n"
            "2. list_relationships(status='proposed') and decide each with set_relationship; add relationships for "
            "unlinked_id_columns whose meaning is clear; set is_default where a table has several roles.\n"
            "3. Loop: get_modeling_worklist → save_table_terms for every table returned, until remaining is 0.\n"
            "4. suggest_sql_attributes → save_sql_attribute for the ones with business meaning.\n"
            "5. Summarize what you built and list what a person should review before certifying.\n\n"
            + guidance.TERM_RULES
        )

    @mcp.prompt(name="answer_question", description="Answer a data question through the ontology.")
    def answer_question_prompt(question: str) -> str:
        return (
            f"Answer this question from the data: {question}\n\n"
            "Follow: check_answerable → get_query_context → lookup_values (for literals) → write SQL → validate_sql → "
            "run_sql → check the rows → answer with the SQL.\n\n"
            + guidance.SQL_RULES
            + "\n"
            + guidance.ANSWER_CHECKLIST
        )

    @mcp.prompt(name="remember", description="Store what I shared (files, images, notes) in the knowledge store.")
    def remember_prompt(what: str = "") -> str:
        subject = f" about: {what}" if what else ""
        return (
            f"Read what I shared in this conversation{subject} and store it with the auto-ontology tools.\n"
            "1. search_ontology(datasource='knowledge') for the things involved; reuse existing Terms.\n"
            "2. Facts to count or compare: define_entity if needed, then add_records with source and _evidence.\n"
            "3. Ideas and explanations: save_concept with a definition, a body and typed relations, with the source.\n"
            "4. Tell me what you stored, what you were unsure about, and anything you left out.\n\n"
            + guidance.KNOWLEDGE_RULES
        )

    @mcp.prompt(name="review_ontology", description="Find gaps and risks in the current ontology.")
    def review_ontology_prompt(datasource: str = "") -> str:
        target = f" of '{datasource}'" if datasource else ""
        return (
            f"Review the ontology{target}: call check_readiness, export_ontology and list_relationships(status='all'). "
            "Report Terms whose descriptions do not state their grain, attributes with unclear names, joins with "
            "roles but no default, unlinked identifier columns, measures without SQL attributes, and anything a "
            "person should certify. For the knowledge store also report empty (stub) concepts, records without a "
            "source, likely duplicate records or Terms, and concepts whose bodies contradict each other. "
            "Propose concrete fixes but do not certify or delete anything yourself."
        )

    @mcp.resource("ontology://guide", name="guide", description="How to use this server.", mime_type="text/markdown")
    def guide() -> str:
        return "\n\n".join(
            [
                "# Auto Ontology MCP",
                guidance.SERVER_INSTRUCTIONS,
                "## Modeling rules\n" + guidance.TERM_RULES,
                "## Relationship review\n" + guidance.RELATIONSHIP_RULES,
                "## SQL attributes\n" + guidance.SQL_ATTRIBUTE_RULES,
                "## Writing SQL\n" + guidance.SQL_RULES,
                "## Checking answers\n" + guidance.ANSWER_CHECKLIST,
                "## Storing knowledge\n" + guidance.KNOWLEDGE_RULES,
            ]
        )

    @mcp.custom_route("/health", methods=["GET"])
    async def health(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "version": __version__})

    mcp.service = svc  # type: ignore[attr-defined]
    return mcp
