<div align="center">

<img alt="Auto Ontology MCP: a semantic layer your AI assistant builds and queries. Works with Claude, Codex, OpenClaw, Hermes and any MCP client." src="docs/assets/banner.png" width="100%">

<p>
  <a href="https://github.com/gogoymh/auto-ontology-mcp/stargazers"><img src="https://img.shields.io/github/stars/gogoymh/auto-ontology-mcp?style=social" alt="GitHub stars"></a>
  <a href="https://github.com/gogoymh/auto-ontology-mcp/actions/workflows/ci.yml"><img src="https://github.com/gogoymh/auto-ontology-mcp/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache--2.0-blue" alt="License: Apache-2.0"></a>
  <img src="https://img.shields.io/badge/python-3.12%2B-3776AB?logo=python&logoColor=white" alt="Python 3.12+">
  <a href="https://modelcontextprotocol.io"><img src="https://img.shields.io/badge/MCP-stdio%20%7C%20streamable%20HTTP-8250df" alt="MCP: stdio and streamable HTTP"></a>
</p>

<p>
  <a href="#quick-start">Quick start</a> ·
  <a href="#see-it-in-action">Demo</a> ·
  <a href="#personal-knowledge-base">Knowledge base</a> ·
  <a href="#tools">Tools</a> ·
  <a href="#how-it-works">How it works</a> ·
  <a href="#safety">Safety</a>
</p>

<a href="https://github.com/gogoymh/auto-ontology-mcp"><img src="docs/assets/star.gif" width="380" alt="Animation: a cursor clicks the Star button and it turns into Starred"></a>

</div>

**Auto Ontology MCP** (`auto-ontology-mcp`) brings [NVIDIA Auto Ontology](https://github.com/NVIDIA/auto-ontology)'s semantic layer to any MCP client, for two jobs:

- **Ask your databases.** Point it at a database, and the model you already use (Claude Code, Codex, OpenClaw, Hermes Agent, Cursor, VS Code, or the Claude and ChatGPT apps) builds business Terms, reviews joins and answers questions with SQL that the server checks against the ontology and the data.
- **Keep a personal knowledge base.** Share receipts, statements, papers, book notes or screenshots with your assistant; it reads them and stores the facts as records, the ideas as linked concepts, and where each came from. Later questions get exact numbers and cited sources.

> [!TIP]
> **Use it in place of an LLM Wiki.** In Karpathy's [LLM Wiki](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) pattern the model compiles your sources into markdown pages that you read in Obsidian. Auto Ontology MCP runs the same loop (ingest, query, lint, with an index and a log) on an ontology instead of loose pages: facts go into typed tables you can sum, filter and compare with SQL, ideas become concept pages with typed links, every record, page and link keeps its source and the quote behind it, and `export_wiki` writes it all out as an Obsidian vault with aliases, `[[links]]` and the graph view. [How the two map](#personal-knowledge-base)

Auto Ontology runs its own LLMs (NVIDIA NIM) behind a Next.js, FastAPI and Postgres stack. This project hands every language step (reading, extracting, naming, judging, writing SQL) to the model in your MCP client, so there is no model endpoint to host and no API key to configure. The server keeps the deterministic work: schema extraction, profiling, key inference, validation, storage and read-only execution, in local SQLite files.

## Why

Text-to-SQL fails quietly. The query runs, a number comes back, and nothing says it is wrong.

| ❌ A model reading a raw schema | ✅ The same model with auto-ontology-mcp |
| --- | --- |
| Guesses which of `billing_city_id` and `delivery_city_id` "the customer's state" means | Gets the join path from reviewed relationships, with the default role and the alternatives spelled out |
| Joins `customers.customer_id = orders.order_id` because the integers happen to overlap | `validate_sql` rejects the join and names the one the ontology knows |
| Filters `state_name = 'pennsylvania'` and reports zero revenue | Is told the stored value is `'Pennsylvania'` before running the query |
| Writes `discount_pct > 20` against a column that holds 0 to 0.3 | Is asked whether it meant `0.2` |
| Sums a product price after joining to order lines and inflates the total | Gets a fan-out warning for the one-to-many join |
| Answers "return on ad spend" with whatever revenue column it finds | Hears from `check_answerable` that nothing covers ad spend, and says so |

## See it in action

The repository ships a synthetic retail database for trying this out (11 tables, 3,200 orders). Below is a question asked after the tables were modeled. Tool output is abridged from a real run of the test fixtures; the model's replies are illustrative.

```text
You     What was revenue for the sushi roll flash drive in Pennsylvania in June 2024?

Model   → check_answerable(entities=["revenue", "sushi roll flash drive", "Pennsylvania"])
          verdict: answerable
          revenue                  covered           Order Line › Revenue (order_lines.line_total)
          sushi roll flash drive   covered_by_value  products.product_name = 'USB Food Flash Drive - Sushi Roll'
          Pennsylvania             covered_by_value  states.state_name = 'Pennsylvania'

        → get_query_context(terms=["Order Line", "Product", "State", "Order"])
          join path: order_lines → products
                     order_lines → orders → customers → cities → states
          role note: customers.delivery_city_id = cities.city_id is the default;
                     other roles: customers.billing_city_id (billing)

        → validate_sql(first draft)
          ✗ Join customers.customer_id = orders.order_id is not how the ontology joins these
            tables (orders.customer_id = customers.customer_id).
          ! No row has states.state_name = 'pennsylvania'. Close real values: 'Pennsylvania'.

        → run_sql(fixed query)
          revenue 316.8, order_lines 4
          draft Terms used: City, Customer, Order, Order Line, Product, State

Model   Revenue was 316.80 from 4 order lines (delivery state Pennsylvania, June 2024).
        The Terms behind this answer are still drafts, so treat the definition as unreviewed.
        [SQL shown]
```

And a question the data cannot answer:

```text
You     What was return on ad spend for the sushi roll flash drive?

Model   → check_answerable(...)
          verdict: partially_answerable   coverage: 0.33
          not covered: "return on ad spend", "ad spend"

Model   This database has sales but no advertising spend, so I can't compute ROAS.
        I can give you revenue for that product instead.
```

## What it does

| | |
| --- | --- |
| **Catalog and profiling** | Reads tables, columns, declared keys and comments through SQLAlchemy, then samples rows to record top values, uniqueness, null rates, ranges, date formats and the complete value set of small text enums |
| **Key inference** | Finds undeclared primary and foreign keys by name and confirms them by value containment. Detects roles (`billing_` vs `delivery_`), self references and bridge tables, and lists identifier columns it could not place |
| **Modeling with your model** | Hands the model one table at a time with samples, keys and the modeling rules from Auto Ontology, then validates and stores the Terms and column mappings it proposes |
| **SQL attributes** | Mines reusable filters, measures and join patterns from executed and verified queries and bridge tables, runs each candidate against the database before saving it |
| **Answerability** | Matches the question's entities to Terms, attributes, SQL attributes and glossary entries, and to names stored in the data (products, places, statuses) |
| **Validation** | Unknown columns (with the ontology's name for them), joins the ontology does not know, near-miss literals, percent-vs-fraction thresholds, one-to-many fan-out, and a dry run |
| **Governance** | Draft, certified and deprecated status on Terms and SQL attributes; editing a certified definition returns it to draft. Verified question and SQL pairs feed later answers |
| **Knowledge store** | A writable store for what your assistant reads: entities with typed columns and references, concepts with a markdown body and typed relations, and a source with locator and quote behind every record. An index, a change log and an Obsidian export make it usable as an LLM Wiki |
| **Portable** | Export and import the whole semantic layer as one YAML or JSON document |

Results of the scan on the demo database, as reported by `auto-ontology-mcp demo`:

| Undeclared foreign keys in the demo | Found and auto-accepted | Proposed for review | Reported as unlinked |
| :---: | :---: | :---: | :---: |
| 9 | 7 | 1 (`employees.manager_id`, a self reference) | 1 (`orders.sales_rep_id`, no name match) |

## Quick start

You need [uv](https://docs.astral.sh/uv/getting-started/installation/).

**1. Create the demo database** (or skip to [your own database](#connect-your-database)):

```bash
uvx --from git+https://github.com/gogoymh/auto-ontology-mcp auto-ontology-mcp demo
```

**2. Connect your client.** One click for Cursor or VS Code (both need [uv](https://docs.astral.sh/uv/getting-started/installation/) installed):

<p>
  <a href="https://cursor.com/en/install-mcp?name=auto-ontology&config=eyJjb21tYW5kIjoidXZ4IiwiYXJncyI6WyItLWZyb20iLCJnaXQraHR0cHM6Ly9naXRodWIuY29tL2dvZ295bWgvYXV0by1vbnRvbG9neS1tY3AiLCJhdXRvLW9udG9sb2d5LW1jcCJdfQ%3D%3D"><img src="https://cursor.com/deeplink/mcp-install-dark.svg" alt="Install in Cursor" height="32"></a>
  <a href="https://insiders.vscode.dev/redirect?url=vscode%3Amcp%2Finstall%3F%257B%2522name%2522%253A%2522auto-ontology%2522%252C%2522command%2522%253A%2522uvx%2522%252C%2522args%2522%253A%255B%2522--from%2522%252C%2522git%252Bhttps%253A%252F%252Fgithub.com%252Fgogoymh%252Fauto-ontology-mcp%2522%252C%2522auto-ontology-mcp%2522%255D%257D"><img src="https://img.shields.io/badge/VS_Code-Install_Server-0098FF?style=for-the-badge&logo=visualstudiocode&logoColor=white" alt="Install in VS Code" height="32"></a>
</p>

For the other clients, copy the snippet below. `auto-ontology-mcp config` prints them with absolute paths filled in, which desktop apps need when `uvx` is not on their `PATH`.

<details open>
<summary><b>Claude Code</b></summary>

```bash
claude mcp add auto-ontology -- uvx --from git+https://github.com/gogoymh/auto-ontology-mcp auto-ontology-mcp
```
</details>

<details>
<summary><b>Codex CLI</b></summary>

Add to `~/.codex/config.toml`:

```toml
[mcp_servers.auto-ontology]
command = "uvx"
args = ["--from", "git+https://github.com/gogoymh/auto-ontology-mcp", "auto-ontology-mcp"]
```
</details>

<details>
<summary><b>OpenClaw</b></summary>

Save the server in OpenClaw's MCP registry ([docs](https://docs.openclaw.ai/cli/mcp/registry)). OpenClaw asks for the local Gateway to be stopped while saved servers change, so start it again afterwards:

```bash
openclaw mcp set auto-ontology '{"command":"uvx","args":["--from","git+https://github.com/gogoymh/auto-ontology-mcp","auto-ontology-mcp"]}'
openclaw mcp list
```

The tools show up in OpenClaw's normal coding and messaging tool profiles.
</details>

<details>
<summary><b>Hermes Agent</b></summary>

Add to `~/.hermes/config.yaml` ([docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp)), then run `/reload-mcp` in a session:

```yaml
mcp_servers:
  auto_ontology:
    command: "uvx"
    args: ["--from", "git+https://github.com/gogoymh/auto-ontology-mcp", "auto-ontology-mcp"]
```

Tools are registered as `mcp_auto_ontology_<tool>`, for example `mcp_auto_ontology_run_sql`. Hermes passes stdio servers only a baseline environment, so if you keep the store somewhere other than `~/.auto-ontology-mcp`, set it under `env:` (`AUTO_ONTOLOGY_HOME: /path`).
</details>

<details>
<summary><b>Cursor</b></summary>

Add to `~/.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "auto-ontology": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/gogoymh/auto-ontology-mcp", "auto-ontology-mcp"]
    }
  }
}
```
</details>

<details>
<summary><b>VS Code</b></summary>

Add to `.vscode/mcp.json`:

```json
{
  "servers": {
    "auto-ontology": {
      "type": "stdio",
      "command": "uvx",
      "args": ["--from", "git+https://github.com/gogoymh/auto-ontology-mcp", "auto-ontology-mcp"]
    }
  }
}
```
</details>

<details>
<summary><b>Claude Desktop</b></summary>

Add to `claude_desktop_config.json` and restart Claude Desktop:

```json
{
  "mcpServers": {
    "auto-ontology": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/gogoymh/auto-ontology-mcp", "auto-ontology-mcp"]
    }
  }
}
```
</details>

<details>
<summary><b>ChatGPT and claude.ai (remote)</b></summary>

Web clients only reach MCP servers on a public HTTPS URL. Run the HTTP transport with a token and expose it through a tunnel:

```bash
uvx --from git+https://github.com/gogoymh/auto-ontology-mcp auto-ontology-mcp \
  serve --transport http --port 8765 --token "$(openssl rand -hex 24)"
cloudflared tunnel --url http://127.0.0.1:8765      # or: ngrok http 8765
```

Add `https://<tunnel-host>/<token>/mcp` as a custom connector with no authentication. Connector settings in these apps take a URL but no custom headers, so the token can sit in the path. Clients that do send headers can use `Authorization: Bearer <token>` or `?key=<token>`. In ChatGPT, write tools need a connector added in developer mode; menu names change between releases.

The `search` and `fetch` tools follow the shape ChatGPT expects from connectors used for search and deep research.
</details>

**3. Ask.** Try these in order:

1. *"Use auto-ontology to build the ontology for the demo datasource: review the proposed joins, model every table, then save useful SQL attributes."* (or pick the `build_ontology` prompt)
2. *"Which states brought in the most delivered revenue in 2024?"*
3. *"What was return on ad spend last quarter?"* The answer should be that the data cannot say.
4. Attach a receipt, a statement or your notes on a book and say *"Remember this."* (or pick the `remember` prompt). See [Personal knowledge base](#personal-knowledge-base).
5. *"Export my knowledge base for Obsidian."* `export_wiki` writes a vault to `~/.auto-ontology-mcp/wiki/knowledge`; open that folder in Obsidian.

## Connect your database

```bash
uvx --from git+https://github.com/gogoymh/auto-ontology-mcp --with "psycopg[binary]" auto-ontology-mcp \
  add shop "postgresql+psycopg://readonly_user:pass@host:5432/db" --schemas sales,geo
```

Add the same `--with` to the client configuration's `args` so the server can load the driver.

You can also give the model a URL and let it call `add_datasource`. Any SQLAlchemy URL works:

| Database | URL | Driver (`uvx --with`) |
| --- | --- | --- |
| SQLite | `sqlite:////absolute/path/file.db` | built in |
| PostgreSQL | `postgresql+psycopg://user:pass@host:5432/db` | `psycopg[binary]` |
| MySQL / MariaDB | `mysql+pymysql://user:pass@host:3306/db` | `pymysql` |
| DuckDB | `duckdb:////absolute/path/file.duckdb` | `duckdb-engine` |

From a clone, `uv sync --extra postgres` (or `mysql`, `duckdb`, `all`) installs the same drivers.

The test suite runs against SQLite and PostgreSQL 17. MySQL and DuckDB go through the same SQLAlchemy and sqlglot code paths but have not been tested against a live server yet.

## Personal knowledge base

Your assistant can already read PDFs, photos, web pages and notes. What it lacks is a place to keep what it read, in a form it can count, compare and cite later. The knowledge store is that place, built from the same parts as Auto Ontology's semantic layer:

| Part | What it holds | Example |
| --- | --- | --- |
| **Entity** | Facts you will count, sum, filter or compare: a Term with its own table, typed columns and references to other entities | `Receipt`: paid_on, merchant → `Merchant`, category, total_krw |
| **Concept** | Meaning: a one-line definition, a markdown body and typed relations (`is_a`, `part_of`, `related_to`, `causes`, ...) | `Spaced Repetition` is_a `Learning Technique`, related_to `Forgetting Curve` |
| **Source** | Where each record, concept and relation came from, with a locator and the quote behind it | `IMG_2041.jpg`, line 3; *Make It Stick*, ch. 2 |

The store is created the first time the model saves something (datasource `knowledge`, file `~/.auto-ontology-mcp/knowledge.sqlite`). Everything else in this README applies to it as well: synonyms in your own language, the glossary, SQL attributes for definitions like "eating out", validation, certification and verified example queries.

### A session

The tool calls and results below are the ones in [`tests/test_knowledge.py`](tests/test_knowledge.py); what the user and the model say is illustrative.

```text
You     [two receipt photos] Remember these.

Model   → search_ontology("receipt", datasource="knowledge")              no match yet
        → define_entity("Merchant", columns=[name (key), city])
        → define_entity("Receipt", table="receipts", synonyms=["영수증"],
              columns=[paid_on date required, merchant ref:Merchant, category, total_krw integer])
        → add_records("Receipt", source={"title": "IMG_2041.jpg", "kind": "image"}, records=[
              {"paid_on": "2026/10/03", "merchant": "을지로 국수집", "category": "Eating Out",
               "total_krw": "12,000원", "_evidence": "합계 12,000"}, ...])
          inserted 3 · created Merchant: 을지로 국수집, Chipotle
          (dates and amounts normalized; adding the same receipt again changes nothing)

You     [book notes] Keep my notes on chapter 2 of Make It Stick.

Model   → save_concept("Spaced Repetition", description="Reviewing material at increasing intervals.",
              body="...", relations=[is_a Learning Technique, related_to Forgetting Curve],
              source={"title": "Make It Stick", "kind": "book"}, locator="ch. 2")
          empty concepts created to fill in later: Learning Technique, Forgetting Curve

You     How much did I spend eating out in October, and where is that from?

Model   → check_answerable(["영수증", "Eating Out"], datasource="knowledge")    answerable
        → run_sql("SELECT SUM(total_krw) FROM receipts
                   WHERE category = 'Eating Out' AND paid_on LIKE '2026-10-%'")   27500
        → get_source(1)      IMG_2041.jpg: 3 Receipt and 2 Merchant records

Model   27,500 KRW, from two receipts in IMG_2041.jpg.
```

If a record in a batch is wrong (a date written as "last tuesday", an amount as "12%", an unknown column), nothing in the batch is saved and every problem is listed, so the model fixes and retries. Records that other records reference cannot be deleted, an entity with records is only dropped with `force`, and column types cannot change.

### Using it as an LLM Wiki

Every part of the [LLM Wiki](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) pattern has a counterpart here:

| LLM Wiki | Auto Ontology MCP |
| --- | --- |
| Raw sources the model reads but never edits | Sources: title, path or URL, summary and the transcribed excerpt, linked from every record, concept and relation |
| Wiki pages the model writes | Concepts (definition, markdown body, typed relations) and entities (records with typed columns) |
| Schema file (`CLAUDE.md`, `AGENTS.md`) | Rules the server sends with its handshake and tools, plus the `remember` prompt |
| Ingest | `remember` → `define_entity`, `add_records`, `save_concept` |
| Query | `search_ontology` (Terms, bodies, sources, record values), `get_term`, `check_answerable`, `run_sql` |
| Filing good answers back | `save_concept(append_body=...)`, `save_example_query` |
| Lint | `review_ontology` prompt; `check_readiness` counts empty concepts and records without a source |
| `index.md` | `list_terms` |
| `log.md` | `recent_changes` |
| Obsidian as the viewer | `export_wiki`: a vault with one page per Term (synonyms as aliases, relations as `[[links]]`), one per source, plus `index.md` and `log.md` |

What the structure adds on top of markdown pages:

- **Numbers you can trust.** "How much on eating out in October?" is a SQL sum over typed records, checked by `validate_sql`, not arithmetic over prose.
- **Provenance per fact.** Each record carries its source, locator and quote, so an answer can say which receipt or page it came from.
- **No silent duplicates or broken data.** Keys and duplicate detection stop the same receipt from being stored twice, and invalid dates, amounts or columns reject the whole batch with reasons.
- **Typed links.** `is_a`, `part_of`, `causes` and the like instead of bare backlinks, so "what are the parts of X" has an answer.
- **Review states.** Draft, certified and deprecated, as in Auto Ontology, so you know which definitions a person confirmed.

Limits to know about:

- The Obsidian vault is a one-way view. Edits made in Obsidian are not read back; change knowledge through the assistant and run `export_wiki` again (it replaces only the pages it generated).
- Search is lexical. Add the words you use as synonyms so they match.
- Original files are not copied. A source keeps its title, path or URL, a summary and the excerpt the model transcribed.
- It is a single-user store on your machine. Back it up by copying `~/.auto-ontology-mcp`.

Large existing tables (bank exports, workout logs) load faster with [sqlite-utils](https://sqlite-utils.datasette.io/) into their own database, connected read-only next to the store:

```bash
uvx sqlite-utils insert ~/kb/personal.db spending ~/Downloads/card-export.csv --csv
uvx --from git+https://github.com/gogoymh/auto-ontology-mcp auto-ontology-mcp add personal "sqlite:///$HOME/kb/personal.db"
```

### From a chat app, with OpenClaw or Hermes Agent

[OpenClaw](https://docs.openclaw.ai) and [Hermes Agent](https://hermes-agent.nousresearch.com) are personal agents you run on your own machine or server and talk to from messaging apps. With auto-ontology-mcp registered ([setup](#quick-start)), you can send a photo from your phone and ask about it weeks later. The tools are the same; Hermes prefixes them with the server name:

```text
You     [photo of a receipt] lunch today

Agent   → mcp_auto_ontology_search_ontology(query="receipt", datasource="knowledge")
        → mcp_auto_ontology_add_records(entity="Receipt", records=[{...}],
              source={"title": "chat photo, 2026-11-06", "kind": "image"})

You     eating out this month vs last month?

Agent   → mcp_auto_ontology_check_answerable(...) → mcp_auto_ontology_run_sql(...)
```

Reading the photo is up to the agent's model, so use one that accepts images. The server runs where the agent runs: `~/.auto-ontology-mcp` is that machine's home directory.

## Tools

| Stage | Tools |
| --- | --- |
| Status | `check_readiness` |
| Connect and scan | `add_datasource`, `scan_datasource` |
| Model | `get_modeling_worklist`, `save_table_terms`, `update_term`, `save_glossary` |
| Relationships | `list_relationships`, `set_relationship`, `find_join_path` |
| SQL attributes | `suggest_sql_attributes`, `save_sql_attribute` |
| Explore | `search_ontology`, `get_term`, `describe_table`, `lookup_values` |
| Answer | `check_answerable`, `get_query_context`, `validate_sql`, `run_sql`, `save_example_query` |
| Knowledge store | `define_entity`, `add_records`, `edit_records`, `save_concept`, `get_source`, `list_terms`, `recent_changes`, `export_wiki` |
| Share | `export_ontology`, `import_ontology` |
| ChatGPT connectors | `search`, `fetch` |

Prompts: `build_ontology`, `answer_question`, `remember`, `review_ontology`. Resource: `ontology://guide`. The server also sends its workflow as MCP instructions at handshake, so the model knows to check answerability and validate before it runs anything.

Read tools carry `readOnlyHint`, and tools that can delete carry `destructiveHint`, so clients can skip or require confirmation accordingly.

## How it works

```mermaid
flowchart LR
    U([You]) <--> M["Your MCP client's model<br/>Claude Code, Codex, OpenClaw,<br/>Hermes, Cursor, ...<br/>names Terms, reviews joins,<br/>extracts entities, writes SQL"]
    M <-- "MCP<br/>stdio or HTTP" --> S["auto-ontology-mcp<br/>scans, profiles, infers keys,<br/>validates, stores, runs read-only"]
    S <--> O[("ontology.db<br/>Terms, relationships,<br/>SQL attributes")]
    S <-- "validated writes" --> K[("knowledge.sqlite<br/>records, sources")]
    S -- "read-only" --> D[("Your databases")]
```

Each step of Auto Ontology maps to one side of that line:

| Step | NVIDIA Auto Ontology | auto-ontology-mcp |
| --- | --- | --- |
| Schema extraction and profiling | Ingestion service | Server: `scan_datasource` |
| Undeclared foreign keys | LLM picks candidate columns, vector search finds the target key, a SQL probe checks values | Server: name rules plus the same value probe; ambiguous cases go to the model |
| Terms and column mapping | LLM, one table at a time | The model, with the same rules, via `get_modeling_worklist` and `save_table_terms` |
| SQL attribute suggestions | Expressions mined from the warehouse's query history, judged by an LLM | Server mines the queries run through it and the verified examples (not the warehouse's own history), the model judges, the server test-runs before saving |
| Joins seen in query history | Recorded from the warehouse's query logs | Joins used by executed and verified queries that are not relationships yet, listed by `list_relationships` for review |
| Answerability | LLM entity extraction plus embedding search | The model extracts entities; the server matches them lexically and against stored values |
| Writing SQL | LLM agent | The model |
| Checking SQL | Database probes plus an LLM review | Server: database probes and ontology checks in `validate_sql` |
| Storage | Postgres and pgvector | Local SQLite files |
| Ingesting knowledge | Ingestion service reads databases | The model reads files, images and notes; the server validates and stores the records, concepts and sources it extracts |

Search is lexical (token overlap, trigram similarity, phrase match) because there is no embedding model. The server instructions ask the model to search with the schema's vocabulary, translate non-English phrases, and add the user's own words as synonyms with `update_term(add_synonyms=[...])`, after which they match directly.

Not carried over from Auto Ontology: the web UI and REST API, users, groups and RBAC, zones (access scoping), tags and rules, PII detection, JSONB path checks, embedding search and reranking, chart generation, KumoRFM predictions and Apache Ossie interchange (export and import use this project's own YAML format). Added here and not in Auto Ontology: write access over MCP, the personal knowledge store (Auto Ontology ingests databases, not documents), concept pages with typed relations, provenance on records, and the Obsidian export.

## Safety

The server writes in exactly one place: `~/.auto-ontology-mcp/knowledge.sqlite`, through the knowledge tools, which build parameterized statements from validated input. `run_sql` cannot write there either. Connected databases are only ever read, enforced in three layers:

1. **SQL guard.** One `SELECT`, `WITH` or set operation per call. DML, DDL, `PRAGMA`, `ATTACH`, `SELECT … INTO`, `FOR UPDATE` and functions that reach files, other servers or server state (`pg_read_file`, `dblink`, `LOAD_FILE`, `set_config`, `load_extension`, ...) are refused. Row sources are allow-listed against the scanned catalog, which also blocks `sqlite_master`, DuckDB's `FROM '/path/to/file'` and `OPENQUERY`.
2. **Read-only connections.** SQLite opens with `mode=ro`, PostgreSQL with `default_transaction_read_only`, DuckDB with `read_only`, MySQL with a read-only session.
3. **Rollback.** Every transaction is rolled back.

Queries time out after 30 seconds and return at most 500 rows (100 by default).

Over HTTP, a token is required to reach the server from anywhere but localhost: without one, requests addressed to any other host are refused as DNS-rebinding protection, which also means a tunnel will not work until you set `--token`.

> [!WARNING]
> Datasource URLs, passwords included, are stored in plain text in `~/.auto-ontology-mcp/ontology.db`. Tool output masks them. Use a read-only database account, and on shared machines set `AUTO_ONTOLOGY_ALLOW_DATASOURCE_TOOLS=0` so datasources can only be added from the CLI.

## Configuration

<details>
<summary>Environment variables</summary>

| Variable | Default | Meaning |
| --- | --- | --- |
| `AUTO_ONTOLOGY_HOME` | `~/.auto-ontology-mcp` | Where the ontology store, the knowledge store (`knowledge.sqlite`) and the demo database live |
| `AUTO_ONTOLOGY_STORE` | `$AUTO_ONTOLOGY_HOME/ontology.db` | Ontology store file |
| `AUTO_ONTOLOGY_MAX_ROWS` | `500` | Upper bound on rows `run_sql` returns |
| `AUTO_ONTOLOGY_QUERY_TIMEOUT_S` | `30` | Per-query timeout in seconds |
| `AUTO_ONTOLOGY_PROFILE_ROWS` | `1000` | Rows sampled per table while profiling |
| `AUTO_ONTOLOGY_ALLOW_DATASOURCE_TOOLS` | `1` | `0` hides `add_datasource` and `scan_datasource` from MCP |
| `AUTO_ONTOLOGY_MCP_TOKEN` | unset | Token required by the HTTP transport (same as `--token`) |
| `AUTO_ONTOLOGY_MCP_HOST` / `_PORT` | `127.0.0.1` / `8765` | HTTP bind address |
</details>

<details>
<summary>CLI</summary>

```text
auto-ontology-mcp                      stdio MCP server (what desktop clients launch)
auto-ontology-mcp serve --transport http --port 8765 --token ...
auto-ontology-mcp demo                 create and register the demo database
auto-ontology-mcp add NAME URL         register and scan a datasource
auto-ontology-mcp scan NAME            re-scan; reviewed joins and Terms are kept
auto-ontology-mcp list                 readiness of every datasource
auto-ontology-mcp export NAME -o ontology.yaml
auto-ontology-mcp export-wiki -o ~/vault/ontology   Obsidian vault of the knowledge store
auto-ontology-mcp import NAME ontology.yaml
auto-ontology-mcp remove NAME
auto-ontology-mcp config               print client configuration snippets
```
</details>

## Development

```bash
git clone https://github.com/gogoymh/auto-ontology-mcp && cd auto-ontology-mcp
uv sync
uv run pytest
```

The PostgreSQL tests run when `AO_TEST_PG_URL` points at a disposable database:

```bash
docker run -d --rm --name aomcp-pg -e POSTGRES_PASSWORD=pw -p 55432:5432 postgres:17
AO_TEST_PG_URL=postgresql+psycopg://postgres:pw@127.0.0.1:55432/postgres uv run --extra postgres pytest
```

| Module | Responsibility |
| --- | --- |
| `catalog.py`, `profiling.py`, `inference.py` | Schema extraction, profiling, key and bridge-table inference |
| `knowledge.py` | The writable knowledge store: entities, records, concepts, relations, sources |
| `wiki.py` | Obsidian vault export |
| `service.py` | Everything behind the tools |
| `validation.py`, `sqlcheck.py` | Read-only guard, column lineage, probe-based validation |
| `search.py`, `graph.py` | Lexical search, join paths |
| `guidance.py` | Rules handed to the model, adapted from Auto Ontology's prompts |
| `server.py`, `http_auth.py`, `__main__.py` | MCP surface, HTTP token, CLI |

## Support the project

If Auto Ontology MCP saved you from a wrong number or replaced a pile of notes, a star on GitHub is the simplest way to help: it is how other people find the project. Bug reports and ideas are welcome in [Issues](https://github.com/gogoymh/auto-ontology-mcp/issues).

<p align="center">
  <a href="https://github.com/gogoymh/auto-ontology-mcp"><img src="docs/assets/star.gif" width="380" alt="Animation: a cursor clicks the Star button and it turns into Starred"></a>
</p>

## Acknowledgements

Built on the ideas and code of [NVIDIA Auto Ontology](https://github.com/NVIDIA/auto-ontology) (Apache-2.0): its modeling, relationship, SQL attribute, entity extraction and SQL rules, column profiling, expression mining and database probes. [NOTICE](NOTICE) lists what came from which file.

Uses [FastMCP](https://github.com/jlowin/fastmcp), [SQLAlchemy](https://www.sqlalchemy.org/) and [sqlglot](https://github.com/tobymao/sqlglot).

## Disclaimer

This is a personal open-source project. I built it on my own time and with my own resources, as a contribution to the open-source community.

- It is not affiliated with, sponsored by or endorsed by NVIDIA, or by any company or organization I am associated with. "Auto Ontology" refers to the NVIDIA project this work builds on.
- It is provided "as is", without warranty of any kind, under the [Apache License 2.0](LICENSE). Use it at your own risk.
- You decide what it touches. Review which databases and files you connect, and keep in mind that what the server returns is passed to the model behind your MCP client and handled under that provider's terms.

## License

[Apache License 2.0](LICENSE)
