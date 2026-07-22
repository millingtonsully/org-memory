# Org Memory

**Contents**

1. [What this is](#what-this-is)
2. [What the code does](#what-the-code-does)
3. [How the system is shaped](#how-the-system-is-shaped)
4. [Load-bearing modules and decisions](#load-bearing-modules-and-decisions)
5. [Keyword search, BM25, and Postgres](#keyword-search-bm25-and-postgres)
6. [Bootstrap and day-two ops](#bootstrap-and-day-two-ops)
7. [Integration into production](#integration-into-production)
8. [Agent platform architecture](#agent-platform-architecture)

---

## What this is

Org Memory is a service that stores workplace content as searchable memory and as a governed people and entity graph.

External sync workers push structured change events into an ingress API. Tools call search and the graph endpoints. The service has its own Postgres database and its own object store for raw envelopes, separate from a customer's application database.

---

## What the code does

The API accepts ChangeEnvelope JSON (the ingest contract for one content change). It archives the raw payload, writes documents and text chunks into Postgres, and enqueues background jobs for embeddings and graph extraction. When someone searches, the service embeds the query, fetches vector and keyword candidates under access-control filters, merges those ranked lists with reciprocal rank fusion, then reranks with a cross-encoder. A separate worker process drains the job queue. Embedding and chat vendor calls run in that worker, not inside the HTTP request path.

---

## How the system is shaped

**Top-level packages under** `src/org_memory/`


| Package              | Role                                                              |
| -------------------- | ----------------------------------------------------------------- |
| `api/`               | HTTP routes, auth dependencies, tool wire shapes                  |
| `services/`          | Ingest, retrieval, extraction, worldbuilder, proposals, retention |
| `db/`                | SQLAlchemy engine, ORM models, repositories with ACL in SQL       |
| `domain/`            | Pure models, principals, fact lifecycle, job type names           |
| `adapters/`          | HTTP embedder, reranker, synthesizer, Supabase Storage, S3        |
| `ports/`             | Protocols for object store, embedder, reranker, chunk search      |
| `workers/`           | Job poll loop and handlers                                        |
| `core/`              | Settings, wiring, errors, metrics, logging                        |
| `taxonomy_registry/` | Closed YAML schema for predicates and platform field bindings     |


**Contracts** are under `contracts/`. The ChangeEnvelope JSON Schema is the ingest contract. Tool request schemas live under `contracts/tools/`. Example envelopes live under `contracts/fixtures/sync_envelopes/`.

**Schema migrations** are under `alembic/versions/`. On a new database, run `alembic upgrade head`.

---

## Load-bearing modules and decisions

This section explains the important paths and why they look the way they do.

### Ingest (`services/ingest.py`)

A ChangeEnvelope is one content change from a sync worker: upsert, permission update, or delete. Ingest handles one envelope in this order:

1. Compute a versioned blob key from workspace, document id, event time, and payload hash. That key is where the raw JSON will live in the object store.
2. Write Postgres first: create or update the document row, replace chunks and participants, enqueue jobs. For a delete (`ChangeKind.delete`), write a tombstone instead of removing the row. A tombstone keeps the document id in the database, marks the content as deleted, and stops it from appearing in search. That way later sync events can still refer to the same id.
3. Put the raw envelope bytes into the object store under the blob key.
4. If the object store write succeeds and a later step fails before the database transaction commits, delete that blob on a best-effort basis so the store stays aligned with committed rows.

If the object store write fails, the API raises and the database transaction rolls back. Postgres stays the system of record and committed document rows always have a matching archived payload path.

On delete, after the tombstone, ingest also calls `GraphRepository.remove_document_evidence`. That removes this document id from the evidence lists on claims, relationships, and entities (the structured graph facts that cited this source). When a claim or relationship has no remaining evidence documents, the fact lifecycle marks it retracted so the active graph reads stop returning it.

### Retrieval (`services/retrieval.py` and `db/repositories/chunks.py`)

Chunk search builds access control into the SQL query. The viewer's principals (`user:<uuid>` and `group:<uuid>` values) go in the `WHERE` clause, so only allowed chunks are candidates for ranking.

The hybrid path is:

1. Embed the query (OpenAI-compatible `/embeddings`) into a vector; moving to Voyage is better since our re-ranker is on Voyage.
2. Grab vector candidates with pgvector (cosine similarity over an HNSW approximate-nearest-neighbor index). This is the dense half of hybrid search.
3. Fetch keyword candidates with Postgres full-text search (`tsvector` + `ts_rank`) under the same ACL filters in SQL. This is the lexical half; together with step 2 that's pgvector + FTS.
4. Merge the two ranked lists with reciprocal rank fusion (RRF), which just combines two rankings when scores are on different scales.
5. Apply recency decay so newer content ranks a bit higher when other signals are close.
6. Rerank the candidate shortlist with a Voyage cross-encoder.
7. Write a retrieval audit row (who searched, what was returned) for later review.

On `worldbuilder_kb`, `author_canonical_entity_id` looks up a person by canonical id and turns that into author display names and aliases used as an author filter. If the id is unknown, the search returns an empty result set.

### Graph ACL (`db/repositories/graph.py`)

Access control for graph objects uses two rules, depending on the object type.

**Passages (chunks of document text)** use any-visible: if the viewer can see the source document, its chunks can appear in search.

**Facts, entities, claims, and relationships** use all-visible: every document currently listed as evidence for that object must be visible to the viewer. If a claim is backed by one public doc and one private doc, only a viewer who can see both documents gets the claim text or the entity description. Graph reads return only objects whose full evidence set the viewer can see.

### Jobs (`db/repositories/jobs.py`, `workers/run.py`)

Background work is a Postgres job queue. Workers claim the next open row with `FOR UPDATE SKIP LOCKED`, which gives each job to exactly one worker. Enqueue keeps at most one open job of the same type and key (for example one open `embed_chunks` job per document), so repeating the enqueue doesn't create duplicates. When a worker claims a job, the query also filters by `workspace_id` from settings so each process only takes work for its configured workspace.

### Object store ports (`ports/object_store.py`, adapters)

`OBJECT_STORE_BACKEND` selects either `supabase` or `s3`. Each is a full backend you configure and run. The process starts only when credentials for the selected backend are present. S3 puts always set server-side encryption. 

### Tool wire (`api/tool_wire.py`)

Domain services return domain types. The HTTP layer maps those into the MCP and Worldbuilder JSON envelopes the caller already uses. Retrieval stays in domain shapes; transport formatting stays at the edge.

### Auth

`X-Api-Key` authenticates the calling service with a constant-time string compare. `X-Principal-Id` and `X-Principal-Groups` name the human viewer for ACL. The gateway in front of Org Memory is responsible for verifying the human session and filling those principal headers. Treat `SERVICE_API_KEY` as root access. Restrict network reachability to trusted callers, rotate the key on a schedule, and keep it out of logs.

---

## Keyword search, BM25, and Postgres

Search is hybrid: **pgvector + Postgres full-text search (`ts_rank`)**, then RRF merge, then Voyage rerank. Both channels stay inside Postgres (plus the rerank HTTP call).

- **Dense (pgvector):** Embed the query, find nearby chunk vectors (meaning / paraphrase match).
- **Lexical (current):** Match query words against chunk text with Postgres FTS ordered by `ts_rank`, with ACL in the same SQL `WHERE` clause. One database, one index to keep in sync with documents. That's why this path is simple to operate on Supabase.

**BM25** BM25 is a better lexical scorer than `ts_rank` for keyword search. Stock Postgres and managed Supabase don't expose in-database BM25 (that needs something like ParadeDB `pg_search` / a host that allows it). So the honest options are:

1. **Best for simplicity + BM25:** Run Org Memory on a Postgres provider that supports BM25 in the database. Then keyword ranking and ACL stay in one place (same operational story as today's FTS), but with BM25 scores. Swap the keyword SQL (or the `ChunkSearch` port) to that BM25 operator; keep pgvector, RRF, and rerank.
2. **What this repo ships on Supabase:** pgvector + `ts_rank`. Good enough for hybrid search; weaker lexical ranking than BM25; no second index outside Postgres.
3. **BM25 on Supabase without a BM25-capable Postgres:** Run BM25 in the app process; keep a lexical index on disk next to the API/worker, sync it whenever chunks change, rank with BM25, then filter candidates by ACL in Postgres. That works, but you add a second store, multi-process sync (`BM25_INDEX_DIR` shared or rebuild-on-start), and operational failure modes that in-database search does not have.

**Location in code:** Lexical ranking is `ChunkSearchRepository.keyword_candidates` (FTS/`ts_rank`). Dense ranking is the vector candidate query in the same repository. Retrieval consumes ranked hits from those methods.

---

## Setup

Credentials

```
WORKSPACE_ID=
DATABASE_URL=postgresql+psycopg://USER:PASSWORD@HOST:5432/DBNAME
SERVICE_API_KEY=
EMBEDDING_API_KEY=
EMBEDDING_API_URL=https://api.openai.com/v1
EMBEDDING_MODEL=text-embedding-3-small
EMBEDDING_DIMENSIONS=1536
RERANK_API_KEY=
RERANK_API_URL=https://api.voyageai.com/v1
RERANK_MODEL=rerank-2.5
```

Then:

```bash
python -m venv .venv
# Windows: .venv\Scripts\Activate.ps1
pip install -e ".[dev,s3]"
alembic upgrade head
uvicorn org_memory.main:app
python -m org_memory.workers.run
```

Health:

- `GET /healthz` answers "is the process up?" (process liveness only).
- `GET /readyz` checks Postgres (`SELECT 1`) and pings the object store.
- `GET /metrics` returns Prometheus text metrics. Same `X-Api-Key` trust as admin.
- `GET /v1/admin/health` surfaces spend-alert flags, a warning when retention days are unset (`RETENTION_DAYS=0`), and worker lag derived from the jobs table.

Tests:

```bash
pytest -m "not integration and not postgres"
pytest -m postgres   # needs DATABASE_URL
```
---

## Integration into production

This section is for connecting organizational memory to a host agent platform. Org Memory is the replacement for Databricks-backed knowledge-base search. Agent core tool calls(`search_knowledge_base`, `worldbuilder_kb`, `worldbuilder_lookup`) go to this service instead of Databricks.

### How data moves in

Agent workers emit ChangeEnvelope JSON to `POST /ingress/envelope`. Principals in envelopes use `user:<uuid>` and `group:<uuid>`. A delete envelope writes a document tombstone (the id stays, search stops returning the content) and clears that doc from graph evidence. A permission-change envelope updates ACL fields and ACL event times on documents and chunks so later reads use the new visibility. Slack, Gmail, and other connectors stay on the host platform. Org Memory consumes the envelopes those systems produce.

### How agents call memory

Agent Core (or another trusted gateway) verifies the human session itself. It then calls Org Memory with:

- `X-Api-Key: $SERVICE_API_KEY`
- `X-Principal-Id: user:<uuid>`
- `X-Principal-Groups: group:<uuid>,...` when needed
- Admin routes also need `X-Principal-Roles: admin`

Tool routes include `POST /tools/search_knowledge_base`, `POST /tools/worldbuilder_kb`, and `POST /tools/worldbuilder_lookup`.

### How structured fields write back

Org Memory emits pending rows on `GET /v1/taxonomy-proposals` and optionally pushes them when `TAXONOMY_PROPOSAL_WEBHOOK_URL` is set. The host platform should apply with Rails `update_entity` (or equivalent) and persist `evidence_doc_ids` with the field update. Then callback `POST /v1/taxonomy-proposals/{id}/applied` or `/rejected`. Rails apply logic stays in the host platform.

---

## Agent platform architecture

The following is the architecture for an agent that would act on org memory.

### Request path

The Rails GraphQL API authenticates the session, serves config and entity CRUD from PostgreSQL, runs workflows and Sidekiq jobs, and agent sessions. Agent Core is the MCP server. It registers tools, calls Anthropic models, proxies connectors, manages agent memory files (markdown) on S3, and orchestrates the E2B sandboxes. KB search goes to Databricks Managed Vector Search.

### Deployable pieces

| Service | Owns |
|---------|------|
| API (Rails) | GraphQL, config CRUD, entities, workflows, tasks, automations, Sidekiq, Twilio plugin surface |
| Agent Core (TypeScript MCP) | Tool dispatch, LLM orchestration, connector primitives, KB tool routing, S3 memory files, sandbox orchestration |
| E2B Sandbox | Isolated Python and shell for agent tools |
| Databricks Vector Search | Semantic search over imported org content|
| Vapi | Voice assistants and calls |
| Twilio | SMS, voice, numbers |

### Connectors

On-demand API proxies through stored credentials (`connector_api_request`). MCP primitives exist for Slack and Gmail. Knowledge Base import into Databricks is a separate pre-import path. Inbound listeners support generic webhooks, Gmail Pub/Sub, and SES routing into taxonomies.

Connector-level ACL mapping into `org_visible` / `allowed_principals` is thin in the host platform's connector layer today.

### Auth and tenancy

Human auth is session-based in Rails. Sandbox to MCP uses `X-Sandbox-Nonce`. Data is scoped by Customer. Permission groups control task type access. Agent sessions are tracked with execution modes and statuses. Formal service-key impersonation internals are not exposed in the tool catalog.

### Workflows, automations, ontology

Workflows are finite state machines on taxonomies with steps, transitions, and one action per step. Actions include create task, update fields, SMS, Slack, invoke agent, AI field updates, automations, plugins, run_code, and related-entity helpers. Automations are event-driven rules with rate limits.

Ontology is a relational entity-attribute-value system (taxonomies, fields, relationships, enum sets). Entity updates are last-write-wins. Deterministic queries use GraphQL and MCP entity tools. `fulltext_search` uses Postgres full-text search. Probabilistic org-content search is `search_knowledge_base` and Worldbuilder.
