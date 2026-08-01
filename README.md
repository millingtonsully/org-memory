# Org Memory

Org Memory is a workplace memory service. It stores organizational content as
**searchable passages** and as a **governed graph** of people, teams, projects,
glossary terms, claims, and relationships. Agents retrieve **viewer-scoped
context** under the caller's access controls.

Sync systems push structured change events into an ingress API. The default
agent tool is **`retrieve_context`**: one call that composes hybrid passage
search, structured facts, and bounded relationship paths. Lower-level
primitives remain for single-channel needs.

## What we run

| Layer | Role |
| ----- | ---- |
| **Postgres** (with pgvector) | System of record: documents, chunks, graph, jobs, audits, spend |
| **Object store** (Supabase Storage or S3) | Raw ChangeEnvelope blobs — choose one backend and its credentials |
| **Vendor HTTP APIs** | Embeddings, rerank, and optional synthesis, configured in settings |

Org Memory owns this Postgres instance and object store for memory workloads.
Missing required settings or object-store credentials stop the process at boot.
Missing or failing vendor calls on the request path raise errors instead of
substituting invented results.

---

## Data flow

```text
Host connectors / sync workers
        │
        ▼
POST /ingress/envelope  (ChangeEnvelope)
        │
        ├─► Postgres: document, chunks, participants, jobs
        └─► Object store: raw envelope bytes
        │
        ▼
Worker process (job queue in Postgres)
        ├─ embed_chunks          (vendor embedder)
        ├─ extract_graph         (LLM → claims / relationships / entities)
        ├─ identity / conflicts  (merge, exclusivity)
        └─ taxonomy / collaboration refreshes
        │
        ▼
Agent (via trusted gateway)
        │
        ▼
POST /tools/retrieve_context
        ├─ hybrid passages   (vector + Postgres FTS → RRF → optional rerank)
        ├─ query_facts       (subjects / about, as_of / believed_as_of)
        └─ query_paths       (bounded walk, same temporal axes)
```

**Write path (ingest) decisions**

1. Postgres first, then object store. If the blob write fails, the DB
   transaction rolls back so every committed document row has a matching
   archived envelope.
2. Deletes are **tombstones**: the document id stays; search stops returning
   it; evidence lists on graph facts drop that doc; facts with no remaining
   evidence are retracted.
3. Embed and extract run on the **worker**; ingress only accepts and persists
   the envelope.

**Read path (`retrieve_context`) decisions**

1. One query embedding is shared with passage search.
2. Graph expansion uses explicit `subjects` and/or an `about` name resolved
   under ACL; passage hits supply text, and subject seeds come from those
   explicit inputs.
3. Optional `max_tokens` packs the returned context so agents stay inside a
   budget.
4. Diagnostics report which channels contributed so operators can inspect
   fusion and packing behavior.

---

## Default agent tool: `retrieve_context`

`POST /tools/retrieve_context`

| Input | Role |
| ----- | ---- |
| `query` | Required natural-language question |
| `mode` | `vector_first` (default), `graph_first`, or `joint` |
| `subjects` | Explicit `{type, id}` seeds for facts/paths |
| `about` | Viewer-scoped name resolved into subject seeds |
| `as_of` / `believed_as_of` | World-time and belief-time filters; when omitted, compose derives a temporal plan from the query when it can |
| `max_tokens` | Optional packing budget |
| filters | Passage filters (`source_*`, dates, `author`, …) |

**Modes**

- **`vector_first`** — search passages, then expand from subjects / `about`.
- **`graph_first`** — resolve subjects and pull facts/paths, then search.
- **`joint`** — search and graph expansion for the same query together.

Contract: `contracts/tools/retrieve_context.request.schema.json`.

### Primitives

| Tool | Purpose |
| ---- | ------- |
| `POST /tools/search_knowledge_base` | Hybrid passage search (MCP envelope) |
| `POST /tools/worldbuilder_kb` | Same search, Worldbuilder response shape |
| `POST /tools/query_facts` | Claims for one subject |
| `POST /tools/diff_facts` | Snapshot diff of claims between two as-of (or belief) points |
| `POST /tools/query_paths` | Bounded relationship walks |
| `POST /tools/worldbuilder_lookup` | Synthesized person/team/project/glossary profile |
| `POST /tools/search_procedural_memory` | Procedural memory search |

Auth: `X-Api-Key` for the calling service; `X-Principal-Id` /
`X-Principal-Groups` for the human viewer. Treat `SERVICE_API_KEY` as root.

---

## Design decisions and tradeoffs

**Compose over a second product surface.** Agents get one default tool
(`retrieve_context`) built from primitives (`search`, `query_facts`,
`query_paths`). Callers pass subjects / `about` when they want graph
expansion alongside passages.

**Postgres as the memory store.** Documents, vectors, FTS, graph, and jobs live
in one database operators already run (including managed Postgres with
pgvector). Path walks enforce all-visible evidence ACL inside the recursive
SQL so private edges never consume the path budget.

**Hybrid search inside Postgres.** Dense (pgvector / HNSW) plus lexical
(Postgres full-text search with `ts_rank`), merged with reciprocal rank fusion,
then an optional cross-encoder rerank. Recency decay nudges newer content when
scores are close. Ranking stays in-repo; vendor embed/rerank errors surface as
failures.

**Parent/child chunks + content-hash carry-over.** Children are embedded and
ranked; parents supply readable section text. Unchanged chunk text under the
same embedding model **reuses** stored vectors on re-ingest. Changing embedding
model or dimensions re-embeds affected chunks.

**Bi-temporal facts.** World time (`valid_from` / `valid_to`) and belief time
(`recorded_at` / `invalidated_at`) are first-class on claims and
relationships. Superseded facts stay for as-of reads. Document `event_time`
grounds extracted windows; optional `time_grain` records source precision.
Registry-exclusive slots close on the write path so “current” answers see one
winner. `retrieve_context` accepts `as_of` / `believed_as_of`, and derives a
temporal plan from the query when those are omitted (ambiguous questions
return structured ambiguity). Ledger: `docs/temporal-model.md`. Pipeline:
`docs/temporal-truth.md`.

**Two ACL rules.** Passages use **any-visible** (see the doc → see its chunks).
Claims/relationships/entities use **all-visible** (every evidence doc must be
visible). Partial evidence omits the fact from that viewer's graph reads.

**Closed taxonomy registry.** Extraction and promotions bind to registry
predicates and types on the write path.

**Single squashed Alembic revision (`0001`).** Schema changes edit that file
in place. Already-migrated databases are recreated to pick up in-place DDL
(indexes, columns); CI and Docker always start fresh.

**Object-store backends are peers.** Set `OBJECT_STORE_BACKEND` to `supabase`
or `s3` and supply that backend's credentials.

**Worldbuilder profiles are derived.** Cached by evidence doc set and
re-grounded on every hit so ACL changes reshape the visible profile. Host User
identity flows through the `platform_user` alias bridge instead.

---

## Hybrid search pipeline

1. Embed the query (OpenAI-compatible `/embeddings` by default).
2. Dense candidates: pgvector cosine over HNSW, ACL in SQL.
3. Lexical candidates: Postgres FTS (`tsvector` + `ts_rank`), same ACL.
4. Reciprocal rank fusion (RRF).
5. Recency decay (`half_life_days` / `min_decay`).
6. Cross-encoder rerank when the shortlist is larger than the requested limit.
7. Retrieval audit row (who searched, what returned).

On `worldbuilder_kb`, `about` scopes to documents where a resolved person
participates; `author` filters authorship. Those are distinct.

---

## Bi-temporal graph and freshness

- **World time** — when the fact held in reality.
- **System time** — when the service believed it.

`query_facts`, `query_paths`, and `retrieve_context` accept `as_of` and
`believed_as_of`. When those axes are set, the hybrid fact channel inside
`retrieve_context` uses the same windows (active + superseded). When both are
omitted, compose derives a temporal plan from the query text; when that plan is
ambiguous, a spend-gated synthesis assist may resolve it (or leave ambiguity /
surface vendor errors). Explicit host timestamps always win. Snapshot questions
(“what changed between A and B”) produce a plan with `range_end`; compose then
includes `fact_diffs`, and hosts can call `POST /tools/diff_facts` directly.
World `as_of` also upper-bounds passage `event_time` (belief
`believed_as_of` upper-bounds `updated_at`) when the host omits those filters.
Schema `0001` indexes subject/endpoint plus temporal ranges. Storage:
`docs/temporal-model.md`. Pipeline: `docs/temporal-truth.md`.

Active facts also rank with exponential freshness
(`FACT_FRESHNESS_HALF_LIFE_DAYS` / `FACT_FRESHNESS_MIN_DECAY`; per-predicate
override in the ontology).

---

## Modules

All application code lives under `src/org_memory/`.

### Packages

| Package | Responsibility |
| ------- | -------------- |
| `api/` | FastAPI routes, auth dependencies, tool JSON envelopes |
| `services/` | Product logic: ingest, retrieval, compose, extraction, worldbuilder, proposals, retention |
| `db/` | Engine, ORM (`orm.py`), repositories; ACL in SQL where it belongs |
| `domain/` | Pure models, principals, emails, fact lifecycle, job type names |
| `adapters/` | HTTP embedder, reranker, synthesizer; Supabase Storage; S3 |
| `ports/` | Protocols for object store, embedder, reranker, chunk search |
| `workers/` | Job poll loop; `workers/handlers/` one module per job family |
| `core/` | Settings, wiring, errors, metrics, logging |
| `taxonomy_registry/` | Load/validate closed knowledge ontology JSON |

Supporting trees: `contracts/` (ChangeEnvelope, taxonomy meta-schema, tool
schemas), `config/taxonomy_registry/` (live ontology instances),
`alembic/versions/0001_initial_schema.py` (only schema revision),
`docs/temporal-model.md`.

### Services (read path and write path)

| Module | Does |
| ------ | ---- |
| `services/ingest.py` | Apply one ChangeEnvelope: docs/chunks/jobs + object-store archive |
| `services/chunking.py` | Parent/child text segmentation for documents |
| `services/retrieval.py` | Hybrid search orchestration, diagnostics builders |
| `services/retrieve_context.py` | Compose search + facts + paths; packing |
| `services/facts_query.py` | Shared subject-fact fetch used by HTTP facts and retrieve |
| `services/facts_diff.py` | Two-snapshot subject fact diff (world or belief) |
| `services/temporality/` | Grounding, grain match, intent (+ LLM assist), eager close, diff |
| `services/ranking.py` | RRF / score helpers with deterministic ties |
| `services/extraction.py` | LLM extract loop + apply entities/claims/relationships |
| `services/extraction_windows.py` | Pure overlapping window split for long documents |
| `services/worldbuilder/` | Resolve subject, gather evidence, synthesize profile |
| `services/worldbuilder/resolution.py` | Name → person/entity under viewer ACL |
| `services/worldbuilder/read_source.py` | Load cited docs/records with explicit ok/denied outcomes |
| `services/worldbuilder/profile_structure.py` | Pure parse/ground/seed of profile JSON |
| `services/worldbuilder/synthesis.py` | Cache, LLM call, profile payload |
| `services/worldbuilder/service.py` | Orchestration facade |
| `services/identity_merge.py` / `entity_resolution.py` | Person identity candidates and merges |
| `services/taxonomy_proposals.py` / `promotions.py` | Host field-value proposals and agent promote |
| `services/procedural_memory.py` | Procedural memory create/search |
| `services/retention.py` / `collaboration.py` | Retention purge; collaboration edge aggregation |
| `services/structured_writers.py` | Shared structured write helpers |

### Data access

| Module | Does |
| ------ | ---- |
| `db/orm.py` | SQLAlchemy models aligned with `0001` |
| `db/repositories/documents.py` | Documents, chunks replace, embedding carry-over |
| `db/repositories/chunks.py` | Vector + FTS candidate queries with ACL |
| `db/repositories/graph/` | Graph package: `base`, `search`, `writes`, `traversal` |
| `db/repositories/people.py` | People and aliases |
| `db/repositories/jobs.py` | Enqueue / claim job queue |
| `db/repositories/audit.py`, `versions.py`, `spend.py`, … | Audits, doc versions, token spend, legal hold, proposals |

### Workers

| Handler | Job |
| ------- | --- |
| `handlers/embedding.py` | Embed chunks; refresh identity embeddings |
| `handlers/graph_extraction.py` | Run extraction; enqueue conflict / proposal follow-ups |
| `handlers/identity.py` | Adjudicate person merges |
| `handlers/conflicts.py` | Resolve claim / relationship exclusivity |
| `handlers/proposals.py` | Generate / webhook taxonomy proposals |
| `handlers/collaboration.py` | Rebuild collaboration edges |

### API surface (high level)

| Area | Routes |
| ---- | ------ |
| Ingress | `POST /ingress/envelope` |
| Agent tools | `retrieve_context`, search, facts, paths, worldbuilder, procedural |
| Graph cards | `/v1/graph/persons/...`, `/v1/graph/entities/...` |
| Admin | jobs, spend, legal holds, retention, connectors |
| Proposals / promotions | `/v1/taxonomy-proposals`, `/v1/promotions` |

---

## Setup

Copy `.env.example` to `.env` and fill every required value. The process names
the missing setting and exits if configuration is invalid.

### Docker (recommended)

```bash
cp .env.example .env   # fill in real keys
docker compose up --build
```

Compose starts: Postgres (pgvector) → one-shot `alembic upgrade head` → API on
`http://localhost:8000` → worker. The migrate service must succeed before API
and worker start.

### Local Python

```bash
python -m venv .venv
# Windows: .venv\Scripts\Activate.ps1
pip install -e ".[dev,s3]"
alembic upgrade head
uvicorn org_memory.main:app
python -m org_memory.workers.run
```

### Health

| Endpoint | Meaning |
| -------- | ------- |
| `GET /healthz` | Process is up |
| `GET /readyz` | Postgres `SELECT 1` + object-store ping |
| `GET /metrics` | Prometheus text (API-key trust like admin) |
| `GET /v1/admin/health` | Spend alerts, retention warning, worker lag |

---

## Tests

| Marker / path | What it covers | Needs |
| ------------- | -------------- | ----- |
| `tests/unit/` (`not integration and not postgres`) | Ranking, retrieve modes, packing, extraction windows/ontology, worldbuilder profile structure, handlers smoke, wire shapes, settings, retrieval eval metrics/harness | Nothing external |
| `tests/postgres/` (`-m postgres`) | ACL SQL, chunk embed carry-over, facts/paths temporal contracts, retrieve_context, worldbuilder, temporal indexes | `DATABASE_URL` |
| `tests/integration/` | Real vendor calls when credentials exist | Vendor keys; skipped when absent |

## Retrieval evaluation

Gold questions and expected document/claim ids live in
`evals/retrieval/gold_set.json`. Labels are evaluation-only.

**Offline scoring** (you already have ranked ids):

```bash
python -m org_memory.eval.score_retrieval --predictions evals/retrieval/example_predictions.json
```

**Live eval** (seeds a hermetic workspace, runs `retrieve_context`, scores):

```bash
# DATABASE_URL required. Uses a fixture embedder (planted vectors), not a vendor.
python -m org_memory.eval.run_live
python -m org_memory.eval.run_live --predictions-out /tmp/preds.json
```

What `run_live` does:

1. Creates an isolated `eval-*` workspace in Postgres.
2. Seeds the gold documents, chunks (with planted embeddings), people/entities,
   and claims with the ids named in the gold set.
3. For each gold case, calls `retrieve_context` with the case query / mode /
   subjects / `as_of`.
4. Collects ranked `doc_ids` and `claim_ids`, optionally writes them, and
   prints hit/recall/precision@k and MRR.

Grow the gold set with real questions as you find failures. Re-seed and re-run
before treating Step 12 retrieval changes as improvements.

```bash
pytest -m "not integration and not postgres"          # default local / CI verify
pytest -m "not integration and not postgres" --cov
pytest -m postgres                                    # hermetic SQL; set DATABASE_URL
```

Postgres tests create isolated `hermetic-*` workspaces against a real
database. CI runs unit and postgres jobs against a fresh migrated Postgres.

Because schema is a single squashed `0001`, a database stamped before a schema
edit (for example new temporal indexes) will fail index assertions until you
recreate the DB and run `alembic upgrade head` again.

---

## Production integration

Point host agent tools at **`retrieve_context`** first; keep primitives for
narrow flows.

### Ingest

Workers emit ChangeEnvelope JSON to `POST /ingress/envelope`. Principals use
`user:<uuid>` and `group:<uuid>`. `event_time` must be timezone-aware (UTC
recommended), year ≥ 1990, and not more than one day in the future — it is
`t_ref` for temporal grounding. Permission-change envelopes update ACL
fields and ACL event times (last-writer-wins on those times). Connectors
(Slack, Gmail, …) stay on the host; Org Memory consumes their envelopes.

### Agent calls

Trusted gateway verifies the human session, then calls Org Memory with:

- `X-Api-Key: $SERVICE_API_KEY`
- `X-Principal-Id: user:<uuid>`
- `X-Principal-Groups: group:<uuid>,...` when needed
- Admin routes also need `X-Principal-Roles: admin`

Also: `POST /v1/procedural-memories`, `POST /v1/promotions`,
`GET /v1/graph/persons/by-platform-user/{platform_user_id}`.

### Identity bridge (OM person ↔ host User)

Send a verified ChangeEnvelope identity key with
`namespace: "platform_user"` and `value: "<host User UUID>"` (same UUID as
`user:<uuid>` principals). Stored as `PersonAlias` with
`source_system = identity:platform_user`. Person cards expose
`platform_user_id` when present. Promotions can auto-fill `host_entity_id`
from that alias.

### Taxonomy write-back

Field-value proposals for ontology fields with a `platform_binding`
(`GET /v1/taxonomy-proposals`; optional `TAXONOMY_PROPOSAL_WEBHOOK_URL`).
Knowledge fields (title, manager, team) use this path. Operational
workflow/task mutations stay on the host.

| Write class | Path |
| ----------- | ---- |
| Knowledge fields | `POST /v1/promotions` or proposals → host apply/reject |
| Operational process | Host APIs directly |

**Conflict rule:** login / permissions / assignment → host User wins.
Inferred knowledge inside OM → OM wins until a proposal is applied or
rejected. Worldbuilder profiles are derived read models; the identity bridge
is the `platform_user` alias on persons.

When `TAXONOMY_PROPOSAL_WEBHOOK_SECRET` is set, webhooks carry
`X-Org-Memory-Signature: sha256=<hex>`. Host apply should be idempotent on
`proposal_id` (or slot + value), then callback
`POST /v1/taxonomy-proposals/{id}/applied` or `/rejected`.

Host platforms own workflows, tasks, SMS, automations, and last-writer-wins
storage for host ontology fields. Org Memory supplies memory, graph facts, and
field-value proposals for bound knowledge fields.
