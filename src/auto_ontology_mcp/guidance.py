"""Instructions handed to the host model.

Auto Ontology sends these rules to its own LLM. Here the host model (Claude,
ChatGPT, …) is the LLM, so the same rules travel inside tool results and the
server's handshake instructions. Rules marked "ported" are adapted from
NVIDIA Auto Ontology (Apache-2.0): ``semantic/term_extractor.py``,
``semantic/fk_suggester.py``, ``semantic/sql_attribute_suggester.py``,
``retrieval/entity_coverage/prompts.py``, ``retrieval/text_to_sql/prompts.py``
and ``skills/nvidia-ontology-query/references/query-validation.md``.
"""

SERVER_INSTRUCTIONS = """\
Auto Ontology MCP gives you (the model) a semantic layer over relational databases: \
business Terms mapped to columns, reviewed SQL attributes (measures, filters, joins) and \
verified relationships between tables. The server does the deterministic work (schema \
extraction, profiling, key inference, validation, read-only execution). You do the \
language work that NVIDIA Auto Ontology normally sends to its own LLM.

Answering a data question (always in this order):
1. check_readiness: is there a modeled datasource that can execute SQL?
2. check_answerable(question, entities): extract the entity phrases yourself, see which \
the ontology covers. If something material is not covered, say so and ask; do not guess.
3. get_query_context(terms): columns, sample values, SQL attributes, the join path and \
verified example queries for the Terms you will use.
4. lookup_values for any literal filter (names, statuses, places) you are not sure about.
5. Write the SQL yourself in the datasource dialect, using the join path and SQL attributes.
6. validate_sql, fix every error, take warnings seriously (fan-out, near-miss literals, \
unverified joins, percent-vs-fraction).
7. run_sql with the user's question. Check the rows against the question before you answer, \
show the SQL, and mention truncation and any uncertified (draft) Terms you relied on.
8. If the user confirms the answer is right, save_example_query so the next answer reuses it.

Building an ontology: add_datasource → scan_datasource → get_modeling_worklist / \
save_table_terms until no tables remain → list_relationships / set_relationship to review \
proposed joins → suggest_sql_attributes / save_sql_attribute → certify what a person approved \
with update_term(status="certified"). After a schema change, scan_datasource again: Terms and \
reviewed joins are kept, and get_modeling_worklist returns new tables and modeled tables that \
gained columns (send their current_terms back with the new columns placed).

Keeping personal knowledge (datasource "knowledge", created on first use): when the user shares \
documents, images, notes or facts to remember, read them yourself and store what they contain. \
search_ontology first and reuse existing Terms instead of creating near-duplicates. Facts you \
would count, sum, filter or compare go into entities (define_entity, then add_records with a \
source); explanations, ideas and definitions go into concepts (save_concept) linked by typed \
relations. Documents (articles, papers, meeting notes) keep their full text as the original in \
the Obsidian vault (source.original for a note already there, original_text or original_file to \
save one); the store keeps their summaries and excerpts as passages (save_passages), each with \
its heading or locator and the Terms it is about. When a document changes, save its source again \
(same uri or id) with the new original_text or original_file, which becomes a new note while the \
old one stays, then save_passages(replace=true) with every passage of the new version and check \
get_source for records and concepts that came from it. Every record, concept and passage should \
name its source. Ask before deleting anything. list_terms is the index and recent_changes the log. For \
"what is X / what do I know about X", answer from get_term (definition, body, relations, sources) \
and search_text (passages); for totals, counts and comparisons, use SQL as for any datasource. \
SQL also reads the concept graph and the text: terms, term_relations (walk chains with WITH \
RECURSIVE), term_sources, passages, passage_terms, and the full-text indexes passages_fts, \
terms_fts and sources_fts (MATCH '"word"*', ORDER BY bm25(...)), so one query can combine a text \
search with relations and records. Cite sources (get_source, or join _source_id = sources.id) and \
give the [[link]] to the original. When an answer is worth keeping, file it back: append it to a \
concept's body or save_example_query. export_wiki writes the store as Obsidian pages next to the \
originals.

Search is lexical, not semantic. Search with the words the schema uses; if the user writes in \
another language, translate entity phrases to the ontology's language first, and try synonyms. \
Add the user's own words as Term synonyms with update_term so the next search finds them.
"""

TERM_RULES = """\
Propose business Terms for each table and assign its candidate columns (ported from Auto \
Ontology's term extractor):
1. Strongly prefer ONE Term per table. A second Term is warranted ONLY when the table is a \
true junction between two unrelated entities. Never split one entity into separate Terms for \
its core fields versus metadata, lifecycle or audit fields. If in doubt, use one Term.
2. Term names are user-friendly with spaces between words (Purchase Order, not \
purchase_orders or PurchaseOrder). Use the name business users would say.
3. Assign EVERY candidate column to exactly one Term, with a display_name that is a \
user-friendly label with spaces (Order Date, Total Amount). Foreign-key columns are not \
candidates: they become relationships.
4. If the table name marks it as a variant of another table (ARC, ARCHIVE, HIST, HISTORY, \
STAGING, DELETED, TEMP), prefix every display_name with a qualifying word (Archived …, \
Historical …).
5. description: one or two sentences of business meaning: what one row is (the grain) and \
what population it covers. Use the sample values as evidence; do not speculate.
6. synonyms: only alternate names that are genuinely used (abbreviations, acronyms, the \
user's language). Do not invent.
7. Column descriptions: one factual sentence each, for columns whose meaning is not obvious \
from the name; mention units, codes and formats you can see in the samples.
8. is_junction: true only when each row is an association between at least two entity roles \
(both columns are foreign keys); false for entity tables that merely contain foreign keys, \
detail tables, lookups and event tables with their own identity.
"""

RELATIONSHIP_RULES = """\
Review proposed joins (ported from Auto Ontology's FK suggester and semantic-FK resolver):
- Accept only when you are confident the source column references the target key: names and \
roles agree, types agree, and most source values exist in the target (containment).
- A matching column name alone is not proof of a join, and containment alone is not proof \
either: small integer surrogate keys overlap across unrelated tables.
- Measures, physical measurements, names/labels, timestamps, flags and free text are not \
foreign keys.
- When several columns of one table reference the same target (billing_city_id, \
delivery_city_id), each is a distinct role. Mark the one that represents the unqualified \
join as is_default; the others stay accepted with their role.
- For an identifier column left without a relationship (unlinked_id_columns), add one with \
set_relationship(action="add") when the business meaning is clear (sales_rep_id → \
employees.employee_id), using value_compatible_targets as evidence.
"""

SQL_ATTRIBUTE_RULES = """\
Decide which candidates become SQL attributes (ported from Auto Ontology's SQL attribute \
suggester). A SQL attribute is a named, reusable SQL expression (a filter, aggregation, \
calculation or join pattern) that captures a clear business concept.
1. ALWAYS promote an expression that compares a column against a literal value \
(status = 'active', amount > 1000, type IN ('A','B')): literals encode business thresholds and \
categories. Exception: an integer literal that looks like a surrogate key (id = 12) is a record \
lookup, not business logic.
2. NEVER promote an expression that only compares columns of the same table with no literal or \
cross-table condition (start_date < end_date).
3. NEVER promote IS NULL / IS NOT NULL on a key column (id, *_id): that is LEFT JOIN plumbing. \
IS NULL on a meaningful column (deleted_at IS NULL) is valid.
4. Promote a JOIN condition or UNION when it appears often AND encodes business logic, defines \
a named alias, or unions several join paths into one population (Task Participants).
5. For a bridge table, one SQL attribute that joins through it to all related entities.
For each one give: name (Title Case, spaces), description (what it captures and when to use \
it), expression (exact SQL), kind (measure | filter | dimension | join | query).
Fragments (measure, filter, dimension) reference columns of the Term's own table, qualified by \
the bare table name (order_lines.line_total). Anything spanning tables must be a full SELECT \
(kind = join or query).
"""

ENTITY_RULES = """\
Extract 1-5 entity phrases from the question (ported from Auto Ontology's entity coverage \
prompt):
- Include subject nouns and domain terms (invoice, customer, shipment) and qualified phrases \
(order shipment, employee hire).
- Keep an aggregation word attached when it names a metric (total revenue, average order \
value), but not when it is only how the question is asked ("how many shipments" → shipment).
- Keep a multi-word filter item together (waterproof hiking tent for family camping).
- Keep names that identify something: product names, brands, categories, places.
- Resolve glossary abbreviations to their definition (MRR → monthly recurring revenue).
- Exclude bare verbs, numbers, calendar literals, standalone status adjectives and generic \
words (id, name, type, code, value).
"""

SQL_RULES = """\
Writing the SQL (ported from Auto Ontology's text-to-SQL rules):
- Use only tables and columns from the context. Qualify columns with table aliases.
- Follow the join path hop by hop; never shortcut a multi-hop path by joining its endpoints.
- When a SQL attribute matches the intent, reuse its expression rather than re-deriving it.
- When a verified example query matches, start from it.
- Both sides of a join must have the same type; never match a name or label against an id \
column; join each foreign key to the key it actually references.
- Use explicit INNER/LEFT/RIGHT/FULL JOIN … ON. No NATURAL, LATERAL or comma joins.
- Aggregate detail rows at their own grain before joining them to a parent whose measure you \
also aggregate (fan-out). A ratio of totals is not a mean of ratios.
- Respect value formats shown in the context (date strings, fractions vs percents, codes).
- A filter on a single constant value does not need that column in SELECT.
"""

ANSWER_CHECKLIST = """\
Before presenting an answer (ported from Auto Ontology's query-validation guide):
- Population and exclusions, measure and unit, grain, grouping, time range: does the SQL \
preserve each one the question asked for?
- Joins use approved keys and roles; no one-to-many fan-out under SUM/AVG.
- Rows: recompute simple totals when you can; empty or NULL is not zero; a truncated result \
cannot prove a complete list, ranking or total.
- Lead with what the rows say, then the SQL. Say which Terms were draft (not certified).
- If a material constraint cannot be verified, report the gap instead of a stronger claim.
"""

KNOWLEDGE_RULES = """\
Storing knowledge (Auto Ontology's model applied to personal knowledge):
1. Reuse before you create: search_ontology for the thing first. One Term per real-world kind of \
thing, named in the singular as people say it (Receipt, Book, Person, Workout).
2. Entities hold facts you will count, sum, filter or compare. Give columns snake_case English \
names and the right type (text, integer, number, date, datetime, boolean, ref:<Entity>). Put the \
unit in the name or description (amount_krw, distance_km). Use ref columns for things that recur \
(merchant, author, place) instead of repeating text; add_records creates the referenced rows by \
name. Set keys when a natural identifier exists (isbn, receipt_no) so re-ingesting updates instead \
of duplicating.
3. Concepts hold meaning: a one-sentence definition, a markdown body (what it is, how it works, \
examples, open questions) and typed relations: is_a, part_of, has_part, instance_of, related_to, \
depends_on, causes, used_for, contrasts_with, same_as, example_of. A Term can be both: an entity \
with records and a body that explains it.
4. Provenance on everything: pass source={title, kind, uri, ...} (or a source id) and, per record, \
_locator (page, timestamp, line) and _evidence (the quote or number as written).
5. Documents: the full text belongs in the Obsidian vault as the source's original (original, \
original_text or original_file), not in the store. Save what is worth finding again as passages: \
one idea each, quotes as written or summaries of one section, with the heading or locator and the \
Terms they are about. When a document changed, read all of it again and call save_passages with \
replace=true rather than adding a second set of passages.
6. Never invent values. Leave unknown fields out; record ambiguity in _note.
7. Certified means a person confirmed it. New knowledge is draft; do not certify on your own.
"""
