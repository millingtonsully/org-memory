# Org Memory

Org Memory is a service that stores workplace content as searchable memory and as a governed people and entity graph.

External sync workers push structured change events into an ingress API. Tools call search and the graph endpoints. The service has its own Postgres database and its own object store for raw envelopes, separate from a customer's application database.

---

## How it works

The API accepts ChangeEnvelope JSON (the ingest contract for one content change). It archives the raw payload, writes documents and parent/child text chunks into Postgres, and enqueues background jobs for embeddings and graph extraction. Every chunk stores a hash of its text, so re-ingesting a document only re-embeds the chunks whose text actually changed; unchanged chunks keep their stored vectors. When someone searches, the service embeds the query, fetches vector and keyword candidates under access-control filters, merges those ranked lists with reciprocal rank fusion, then reranks with a cross-encoder when the shortlist is larger than the requested limit. A separate worker process drains the job queue. Ingest defers embed/extract vendor calls to the worker; search, Worldbuilder synthesis, and procedural create/search call vendors on the HTTP request path.

Facts in the graph are bi-temporal: each claim and relationship records when it was true in the world (`valid_from` / `valid_to`) and when the system believed it (`recorded_at` / `invalidated_at`). `query_facts` accepts `as_of` and `believed_as_of` points for both axes. See `docs/temporal-model.md` for the full model.

---

## System shape

**Top-level packages under** `src/org_memory/`


| Package              | Role                                                                |
| -------------------- | ------------------------------------------------------------------- |
| `api/`               | HTTP routes, auth dependencies, tool wire shapes                    |
| `services/`          | Ingest, retrieval, extraction, worldbuilder package, proposals, retention |
| `db/`                | SQLAlchemy engine, ORM models, repositories with ACL in SQL         |
| `domain/`            | Pure models, principals, emails, fact lifecycle, job type names     |
| `adapters/`          | HTTP embedder, reranker, synthesizer, Supabase Storage, S3          |
| `ports/`             | Protocols for object store, embedder, reranker, chunk search        |
| `workers/`           | Job poll loop; handlers live under `workers/handlers/` by concern   |
| `core/`              | Settings, wiring, errors, metrics, logging                          |
| `taxonomy_registry/` | Closed JSON Schema knowledge ontology (types, predicates, bindings) |


**Contracts** are under `contracts/`. The ChangeEnvelope JSON Schema is the ingest contract. The knowledge ontology meta-schema is `contracts/taxonomy_registry.schema.json`; live instances live under `config/taxonomy_registry/` (see `knowledge_ontology.json`). Tool request schemas live under `contracts/tools/`.

**Database schema** lives in a single Alembic revision: `alembic/versions/0001_initial_schema.py`. On a new database, run `alembic upgrade head`. Schema changes are edited into that file in place; this project does not keep a chain of numbered migrations.

**Temporal model** for claims and relationships is documented in `docs/temporal-model.md`.

Agent tools today are separate primitives: `search_knowledge_base` / `worldbuilder_kb` for passages, `query_facts` for structured claims, and `query_paths` for relationship traversal. Compose them in the host or agent until a unified retrieve tool exists.

---

## Modules

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

1. Embed the query (OpenAI-compatible `/embeddings` by default) into a vector.
2. Grab vector candidates with pgvector (cosine similarity over an HNSW approximate-nearest-neighbor index). This is the dense half of hybrid search.
3. Fetch keyword candidates with Postgres full-text search (`tsvector` + `ts_rank`) under the same ACL filters in SQL. This is the lexical half; together with step 2 that's pgvector + FTS.
4. Merge the two ranked lists with reciprocal rank fusion (RRF), which just combines two rankings when scores are on different scales.
5. Apply recency decay so newer content ranks a bit higher when other signals are close.
6. Rerank the candidate shortlist with a Voyage cross-encoder.
7. Write a retrieval audit row (who searched, what was returned) for later review.

On `worldbuilder_kb`, `about` resolves a person and scopes to documents where they participate (any role); `author` / `author_canonical_entity_id` filter to authorship. Those are distinct. Free-string `source_system` / `source_type` filters accept any connector label (not a fixed enum). Recency uses `half_life_days` and `min_decay`. `worldbuilder_lookup` synthesizes structured profiles for `person`, `team`, `project`, and `glossary` under the same ACL rules, and returns queryable graph claims/relationships plus citation ids for `read_source`.

### Graph ACL (`db/repositories/graph.py`)

Access control for graph objects uses two rules, depending on the object type.

**Passages (chunks of document text)** use any-visible: if the viewer can see the source document, its chunks can appear in search.

**Facts, entities, claims, and relationships** use all-visible: every document currently listed as evidence for that object must be visible to the viewer. If a claim is backed by one public doc and one private doc, only a viewer who can see both documents gets the claim text or the entity description. Graph reads return only objects whose full evidence set the viewer can see.

### Jobs (`db/repositories/jobs.py`, `workers/run.py`, `workers/handlers/`)

Background work is a Postgres job queue. Workers claim the next open row with `FOR UPDATE SKIP LOCKED`, which gives each job to exactly one worker. Enqueue keeps at most one open job of the same type and key (for example one open `embed_chunks` job per document), so repeating the enqueue doesn't create duplicates. When a worker claims a job, the query also filters by `workspace_id` from settings so each process only takes work for its configured workspace. Handler modules under `workers/handlers/` cover embedding, graph extraction, identity adjudication, conflict resolution, taxonomy proposals, and collaboration aggregation.

### Object store ports (`ports/object_store.py`, adapters)

`OBJECT_STORE_BACKEND` selects either `supabase` or `s3`. Each is a full backend you configure and run. The process starts only when credentials for the selected backend are present. S3 puts always set server-side encryption. 

### Tool wire (`api/tool_wire.py`)

Domain services return domain types. The HTTP layer maps those into the MCP and Worldbuilder JSON envelopes the caller already uses. Retrieval stays in domain shapes; transport formatting stays at the edge.

### Auth

`X-Api-Key` authenticates the calling service with a constant-time string compare. `X-Principal-Id` and `X-Principal-Groups` name the human viewer for ACL. The gateway in front of Org Memory is responsible for verifying the human session and filling those principal headers. Treat `SERVICE_API_KEY` as root access. Restrict network reachability to trusted callers, rotate the key on a schedule, and keep it out of logs.

---

## Keyword search, BM25, and Postgres

Search is hybrid: **pgvector + Postgres full-text search (**`ts_rank`**)**, then RRF merge, then Voyage rerank when the shortlist is larger than the requested limit. Both channels stay inside Postgres (plus the optional rerank HTTP call). Child chunks are embedded and ranked; parent section text is returned for matched children.

- **Dense (pgvector):** Embed the query, find nearby chunk vectors (meaning / paraphrase match).
- **Lexical (current):** Match query words against chunk text with Postgres FTS ordered by `ts_rank`, with ACL in the same SQL `WHERE` clause. One database, one index to keep in sync with documents. That's why this path is simple to operate on Supabase.

**BM25:** A stronger lexical scorer than `ts_rank`. Stock Postgres and managed Supabase expose full-text search with `ts_rank`, so this repo ships **pgvector +** `ts_rank`. Upgrading lexical ranking later means swapping the keyword SQL (or the `ChunkSearch` port) to a BM25-capable Postgres extension; everything else in the pipeline stays the same.

**Location in code:** Lexical ranking is `ChunkSearchRepository.keyword_candidates` (FTS/`ts_rank`). Dense ranking is the vector candidate query in the same repository. Retrieval consumes ranked hits from those methods.

---

## Setup

Copy `.env.example` to `.env` and fill in the required values. The process refuses to start when a required setting is missing or invalid, and the error names the setting.

### Docker (recommended)

The compose file runs the full stack: Postgres with pgvector, a one-shot migration container, the API, and the worker.

```bash
cp .env.example .env   # fill in real keys
docker compose up --build
```

The API listens on `http://localhost:8000`. The `migrate` service applies the single Alembic revision and exits; the API and worker wait for it.

### Local Python

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
pytest -m "not integration and not postgres"          # unit tests, no services
pytest -m "not integration and not postgres" --cov    # with coverage
pytest -m postgres                                    # hermetic SQL tests; needs DATABASE_URL
```

---

## Integration into production

This section is for connecting organizational memory to a host agent platform. Org Memory is the replacement for Databricks-backed knowledge-base search. Agent core tool calls (`search_knowledge_base`, `worldbuilder_kb`, `worldbuilder_lookup`) go to this service instead of Databricks.

### How data moves in

Agent workers emit ChangeEnvelope JSON to `POST /ingress/envelope`. Principals in envelopes use `user:<uuid>` and `group:<uuid>`. A delete envelope writes a document tombstone (the id stays, search stops returning the content) and clears that doc from graph evidence. A permission-change envelope updates ACL fields and ACL event times on documents and chunks so later reads use the new visibility. Slack, Gmail, and other connectors stay on the host platform. Org Memory consumes the envelopes those systems produce.

### How agents call memory

Agent Core (or another trusted gateway) verifies the human session itself. It then calls Org Memory with:

- `X-Api-Key: $SERVICE_API_KEY`
- `X-Principal-Id: user:<uuid>`
- `X-Principal-Groups: group:<uuid>,...` when needed
- Admin routes also need `X-Principal-Roles: admin`

Tool routes include `POST /tools/search_knowledge_base`, `POST /tools/worldbuilder_kb`, `POST /tools/worldbuilder_lookup`, `POST /tools/query_facts`, `POST /tools/query_paths`, and `POST /tools/search_procedural_memory`. Procedural create is `POST /v1/procedural-memories`. Agent promote-to-taxonomy is `POST /v1/promotions`. Graph person resolve by host user is `GET /v1/graph/persons/by-platform-user/{platform_user_id}`.

### Identity bridge (OM person ↔ host User)

Send a verified ChangeEnvelope identity key with `namespace: "platform_user"` and `value: "<Aviary User UUID>"` (same UUID as `user:<uuid>` principals). Org Memory stores that as `PersonAlias` with `source_system = identity:platform_user`. Person graph cards expose `platform_user_id` when present. Promotions auto-fill `host_entity_id` from that alias when the caller omits it.

### How structured fields write back

Org Memory emits **field-value** proposals for existing ontology fields on `GET /v1/taxonomy-proposals` and pushes them when `TAXONOMY_PROPOSAL_WEBHOOK_URL` is set (blank URL means pull-only). Use this path for knowledge fields (title, manager, team). Operational workflow/task mutations stay on the host.

Proposals bind registry predicates that declare a `platform_binding`. They do not create new host entity types or fields. Bindings come from `config/taxonomy_registry/knowledge_ontology.json`.


| Write class                                 | Path                                                             |
| ------------------------------------------- | ---------------------------------------------------------------- |
| Knowledge fields (title, manager, team, …)  | `POST /v1/promotions` or generated proposals → host apply/reject |
| Operational process (tasks, workflow state) | Host APIs directly — Org Memory does not gate these              |


**Conflict rule:** login / permissions / assignment → host User wins. Inferred knowledge inside OM → OM wins until a proposal is applied or rejected. Worldbuilder profiles are derived; they are not the identity bridge.

**Delivery:** pull `GET /v1/taxonomy-proposals`. When `TAXONOMY_PROPOSAL_WEBHOOK_URL` is set, pending rows are also POSTed; when `TAXONOMY_PROPOSAL_WEBHOOK_SECRET` is set, requests include `X-Org-Memory-Signature: sha256=<hex>` over the exact JSON body. Agent promote: `POST /v1/promotions` creates an OM claim and a pending proposal.


| Field                         | Meaning                                                                     |
| ----------------------------- | --------------------------------------------------------------------------- |
| `proposal_id`                 | OM proposal id                                                              |
| `subject_type` / `subject_id` | OM canonical subject (e.g. person id)                                       |
| `host_entity_id`              | Host entity / User id when known (may be auto-filled from `platform_user`)  |
| `taxonomy_key` / `field_key`  | Host ontology binding                                                       |
| `predicate`                   | Registry predicate key                                                      |
| `value`                       | Proposed field value                                                        |
| `confidence`                  | Claim confidence                                                            |
| `evidence_doc_ids`            | OM document ids supporting the value                                        |
| `source_claim_id`             | OM claim id                                                                 |
| `precedence_class`            | `ground_truth` / `agent_promote` / `extraction_multi` / `extraction_single` |
| `status`                      | `pending` until host callbacks                                              |


**Host apply:** (1) map `subject_id` / `host_entity_id` to the host entity, (2) update `taxonomy_key.field_key`, (3) persist `evidence_doc_ids` for audit, (4) callback `POST /v1/taxonomy-proposals/{id}/applied` or `/rejected` with `decided_by`. One pending proposal per `(subject, taxonomy_key, field_key)`; re-promote supersedes. Host apply should be idempotent on `proposal_id` or slot + value.

Org Memory does not create host workflows, tasks, SMS, or automations, and is not the LWW store for host ontology.



### Claim freshness

`query_facts` and hybrid fact ranking apply exponential freshness using `FACT_FRESHNESS_HALF_LIFE_DAYS` / `FACT_FRESHNESS_MIN_DECAY`. A predicate may set `freshness_half_life_days` in `knowledge_ontology.json` to use a different half-life than the global setting; if the field is absent, the global setting is used. Stale active facts remain stored but rank lower.