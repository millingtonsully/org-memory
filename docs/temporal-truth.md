# Temporal truth for organizational memory

This document explains why time matters for workplace memory, how Org Memory
handles it end to end, and how that design maps onto the codebase.

For the storage model itself (axes, indexes, lifecycle states), see
[`temporal-model.md`](temporal-model.md). This file is the **shipped product
pipeline** for temporal correctness from extract through retrieve.

---

## 1. The problem in plain English

Organizations change. People change titles. Teams reorganize. Policies get
corrected. The same inbox that said “Alice is an IC” in January may say “Alice
is a manager” in June. A wiki may be backdated. An agent that retrieves both
statements as equally “true now” will sound confident and be wrong.

Three different questions get collapsed into one if you only have vectors:

1. **What is true in the world right now?**  
   (“Who is Alice’s manager today?”)
2. **What was true in the world at a past moment?**  
   (“Who was Alice’s manager in March?”)
3. **What did this system believe at a past moment?**  
   (“What title did we report for Alice before the HR correction landed?”)

Those are not the same. (1) and (2) are **world time**. (3) is **belief time**
(system / transaction time). Organizational memory that answers agents well
keeps both clocks, closes old facts when exclusive slots change, grounds
extracted times to a document reference clock, and interprets which clock a
natural-language question is asking about.

Bi-temporal columns in Postgres hold the history. Correctness also requires
write-path discipline, ontology exclusivity, and a query planner that fills
`as_of` / `believed_as_of` (or “current”) from the question—or returns
ambiguity when the question does not choose an axis clearly.

---

## 2. Decomposition

| Layer | Name | Responsibility |
| ----- | ---- | ---------------- |
| **L0** | Ledger | Store world intervals (`valid_from` / `valid_to`) and belief intervals (`recorded_at` / `invalidated_at`); retire facts instead of deleting them |
| **L1** | Ontology exclusivity | Know which predicates/relationships allow one current value vs many |
| **L2** | Write grounding | Attach world-time windows to new facts using document `event_time` as reference (`t_ref`) plus any explicit or relative time in the text |
| **L3** | Supersession | When a new exclusive value wins, close the loser on **both** axes (set `valid_to`, set `invalidated_at`, point `superseded_by_*`) |
| **L4** | Query intent | Map natural language → `{axis, time_point or range, granularity, confidence}` or return ambiguity |
| **L5** | Read filters | Apply the plan to `query_facts`, `query_paths`, hybrid `fact_candidates`, subject claim/edge viewer reads, and compose (`retrieve_context`), including shared “current” = active ∩ validity-now and `as_of_grain` (ACL + temporal filters in SQL) |
| **L6** | Evaluation | Gold covers current, world (host+NL), belief (host+NL), grain, joint axes, multi-valued, and snapshot `fact_diffs` |

Each layer owns a distinct failure class:

- L0 → durable history of what was true and what was believed  
- L1 → exclusive slots vs multi-valued facts  
- L2 → windows anchored to real document time  
- L3 → one active winner per exclusive slot after write  
- L4 → agents need not speak SQL-era parameters  
- L5 → every read channel applies the same plan  
- L6 → regressions stay visible  

---

## 3. Design foundations

These are the rules of the Temporal Truth Pipeline.

**Bi-temporal ledger.** World time and belief time are independent clocks.
Facts are retired with closed intervals; they are not deleted. Org Memory
stores this shape on claims and relationships.

**Deterministic supersession for known-exclusive slots.** Vector similarity
alone cannot tell a contradiction from a paraphrase. Exclusive `(subject,
predicate)` keys close losers in the ledger so “current” reads see one winner.
Winner selection uses precedence and evidence, never an LLM guess about which
value is “right.”

**Document reference time (`t_ref`).** Relative phrases (“two weeks ago,”
“last summer”) resolve against the envelope’s `event_time`. When the text
carries no explicit time, `valid_from` defaults to that event time so every
fact still has a grounded world-time anchor.

**Eager close on the write path.** For registry-exclusive predicates and
relationships, supersession runs when the active fact is applied, inside the
same transaction path. “Current” readers never depend on a later job to hide a
second winner. The async conflict worker remains the safety net for duplicates,
races, promotions, and registry-unknown exclusivity.

**Time granularity as data.** Questions and sources mix day, month, quarter,
and year. Each window carries a `time_grain`. Matching “in March” uses month
containment. The system does not invent `2026-03-01T00:00:00Z` when the source
only said “March.”

**Temporal intent as a plan object.** Natural language maps to
`current` | `world` | `belief`, optional point or range, grain, and
confidence. Low confidence returns structured ambiguity (same spirit as
`about` resolution). The planner fills existing `as_of` / `believed_as_of`
knobs on compose and primitives—one retrieve surface, not a second product.

**One plan on every read channel.** Structured facts, paths, and hybrid fact
candidates all honor the same axes. Compose passes the plan through.

**Scope of this pipeline.** Grounding, exclusive supersession, intent, and
consistent read filters. Multi-valued predicates (`member_of`, skills) stay
multi-valued. Ranking freshness and passage recency remain separate signals;
they complement the ledger rather than replace supersession.

**Shipped temporal coverage.** Temporal gold includes current winner, NL
world-axis, host and NL belief-axis, month grain, joint world+belief, snapshot
diff (`fact_diffs`), and multi-valued `team` cases in
`evals/retrieval/gold_set.json`. Narrative multi-hop time and speech-act
distinctions remain out of this pipeline.

---

## 4. The solution

**Temporal Truth Pipeline:** ledger → ground → close → plan → filter.

```text
                    WRITE PATH                         READ PATH
                    ----------                         ---------
ChangeEnvelope ──► extract (LLM)
   event_time         │
   (= t_ref)          ▼
              ground windows (L2)
              grain + valid_from/to
                      │
                      ▼
              apply claim/edge (L0)
                      │
          ┌───────────┴───────────┐
          ▼                       ▼
   registry exclusive?      multi-valued /
          │                 exclusivity unknown
          ▼                       │
   eager supersede (L3)           ▼
   close losers now         enqueue conflict job
          │                 (LLM exclusivity when needed;
          │                  deterministic winner)
          └───────────┬───────────┘
                      ▼
                 bi-temporal rows

Query ──► temporal intent (L4) ──► TemporalQueryPlan
              │                      axis, point/range, grain,
              │                      confidence | ambiguous
              ▼
         retrieve_context / query_facts / query_paths / fact_candidates (L5)
```

### 4.1 Ledger

- World: half-open `[valid_from, valid_to)`  
- Belief: `[recorded_at, invalidated_at)`  
- Status: proposed → active → superseded | retracted  
- Supersession sets `valid_to` (prefer winner’s `valid_from`), `invalidated_at`,
  and `superseded_by_*`  
  See `GraphClaimsMixin.supersede_claim` and the relationship twin.

### 4.2 Ontology exclusivity

`config/taxonomy_registry/knowledge_ontology.json` marks predicates and
relationship types with `mutually_exclusive`. Examples:

- Exclusive: `title`, `reports_to`, `definition`, relationship `reports_to`  
- Multi-valued: `team` (predicate), `member_of`, `uses_term`  

**Rule:** If `mutually_exclusive` is true, at most one **active** value may
exist for the slot after apply. If false, coexistence is allowed. If unset,
the async path may ask the model for exclusivity only—never for the winner
(`domain/fact_lifecycle.rank_conflict_candidates`).

### 4.3 Write grounding

Inputs:

- `t_ref = document.event_time` (required on envelopes)  
- Structured time fields from the extractor  

Extractor output (additive JSON fields on claims/relationships):

| Field | Meaning |
| ----- | ------- |
| `valid_from` | Instant or null (null → use `t_ref`) |
| `valid_to` | Instant or null (null → open) |
| `time_grain` | `day` \| `month` \| `quarter` \| `year` \| `unknown` |
| `time_expression` | Raw phrase if any (“last March”, “effective Q3”) |

Deterministic grounding module (`grounding.py`):

1. Parse absolute ISO-ish timestamps when present.  
2. Resolve relative phrases against `t_ref` with a small, tested rule table;
   otherwise leave grain coarse and the window wide.  
3. Default: `valid_from = t_ref`, `valid_to = null`; when grain was `unknown`,
   default grain becomes **`day`**.  
4. Prompt guidance asks for grain no finer than the evidence supports (not a
   hard runtime clamp beyond normalize).  
5. Contradictory windows (`from > to`) fail closed: grounding returns `None`
   and apply skips the fact (`dropped_unverifiable`).

`time_expression` is returned on `GroundedInterval` for grounding only; it is
**not** persisted on claim/relationship rows.

The extraction prompt states `t_ref` explicitly so relative language has a
documented clock.

### 4.4 Eager supersession

On successful `add_claim` / `add_relationship` when status is `active` and the
registry marks the key mutually exclusive:

1. Lock the slot (`active_claims_for_slot_locked`).  
2. Rank with `rank_conflict_candidates` (including the new row).  
3. Supersede losers with `valid_to = winner.valid_from`.  
4. Apply the same pattern to exclusive relationships.

Extraction apply, structured field writers, promotions, and glossary seed call
`eager_close_*` for registry-exclusive slots (including same-object duplicate
collapse before exclusive ranking). `resolve_claim_conflict` /
`resolve_relationship_conflict` remain the async safety net for residual
multi-value slots, races, and registry-unknown exclusivity; conflict enqueue
triggers when more than one **active claim row** remains in the slot.

### 4.5 Temporal intent planner

`query text (+ optional now)` → `TemporalQueryPlan`.

```text
TemporalQueryPlan
  axis: current | world | belief
  as_of: datetime | null          # world point (axis=world)
  believed_as_of: datetime | null # belief point (axis=belief)
  range_end: datetime | null      # optional second bound for diff/range
  grain: day | month | quarter | year | unknown
  confidence: float
  status: ok | ambiguous
  rationale: short string for diagnostics
```

Interpretation cues (deterministic first; spend-gated assist when rules
abstain):

| Cue family | Axis |
| ---------- | ---- |
| “now”, “current”, “who is”, no time language | `current` |
| “in March”, “as of”, “during the reorg”, “when she was on X” | `world` |
| “what did we think”, “before the correction”, “as of our records on” | `belief` |
| “what changed between … and …” | two world (or belief) points → `as_of`/`believed_as_of` + `range_end`; compose fills `fact_diffs`; `POST /tools/diff_facts` is the primitive |

Ambiguous questions return structured ambiguity (like `about`). Explicit
client `as_of` / `believed_as_of` always win (host authority). When the client
omits them, `retrieve_context` applies the plan.

### 4.6 Read path

- `query_facts` / `paths_from` filter both axes.  
- Hybrid `fact_candidates` honor axes when the plan (or request) supplies them.  
- When both axes are omitted, all three channels use **active ∩ validity-now**.  
- Compose forwards the plan (including `as_of_grain`) into search and graph
  expansion; range plans also fill `fact_diffs` and passage from/to caps.  
- Diagnostics include `temporal_plan` via `to_diagnostics()` (axis, status,
  grain, confidence, as_of / believed_as_of / range_end, rationale)—never
  denied doc ids.

### 4.7 Evaluation

The temporal gold suite locks L3–L6 behavior:

1. Two titles in sequence → “current” returns only the winner.  
2. Mid-window `as_of` returns the March title, not the June title.  
3. Belief-axis questions (host timestamp and NL planner) return the
   pre-correction belief, including world validity at that belief instant.  
4. Multi-valued `team` keeps both teams.  
5. Host `as_of_grain` and NL range plans exercise grain and `fact_diffs`.  
6. Joint `as_of` + `believed_as_of` AND-filters correctly (host `as_of` for
   world clock).  

Fixtures use real timestamps. Temporal cases assert **graph correctness**;
planted vectors remain appropriate for retrieval wiring tests elsewhere.

---

## 5. How the pipeline answers each concern

| Concern | How the pipeline addresses it |
| ------- | ----------------------------- |
| Both “IC” and “Manager” live as current truth | Exclusive ontology + eager supersession closes the loser on write |
| “In March” answered with today’s title | World-time filter on validity windows; superseded rows remain queryable |
| “What did we believe before the fix?” | Belief-time filter on `recorded_at` / `invalidated_at` |
| Relative language (“two weeks ago”) | Ground against document `event_time` (`t_ref`) |
| Fake precision (“March” → midnight UTC) | Store and match `time_grain` |
| Model picks the “right” title | Winner ranking stays deterministic (`rank_conflict_candidates`) |
| Embeddings surface stale and current equally | Validity and supersession govern the fact channel; exclusive slots are ledger-authoritative |
| Agents omit `as_of` | Temporal intent planner fills a plan or returns ambiguity |
| Multi-valued facts closed by mistake | Registry `mutually_exclusive: false` skips eager close |
| Concurrent writes and promotions | Eager path for known-exclusive keys; async conflict worker as safety net |
| Hybrid search out of sync with facts/paths | Compose passes the same plan into `fact_candidates` |
| Regressions unnoticed | Temporal gold cases on L3–L5 |

---

## 6. Codebase map

### Documentation and contracts

| Path | Role |
| ---- | ---- |
| [`docs/temporal-model.md`](temporal-model.md) | Axes, lifecycle, indexes, freshness |
| [`README.md`](../README.md) | Product summary of bi-temporal retrieve |
| [`config/taxonomy_registry/knowledge_ontology.json`](../config/taxonomy_registry/knowledge_ontology.json) | `mutually_exclusive` flags |
| [`contracts/taxonomy_registry.schema.json`](../contracts/taxonomy_registry.schema.json) | Schema for exclusivity fields |

### Domain and taxonomy

| Path | Role |
| ---- | ---- |
| [`src/org_memory/domain/fact_lifecycle.py`](../src/org_memory/domain/fact_lifecycle.py) | Status machine; `ConflictCandidate`; deterministic `rank_conflict_candidates` |
| [`src/org_memory/taxonomy_registry/models.py`](../src/org_memory/taxonomy_registry/models.py) | `predicate_mutually_exclusive` / `relationship_mutually_exclusive` |

### Write path

| Path | Role |
| ---- | ---- |
| [`src/org_memory/services/extraction.py`](../src/org_memory/services/extraction.py) | LLM window loop; gains explicit `t_ref` and structured time fields |
| [`src/org_memory/services/extraction_apply.py`](../src/org_memory/services/extraction_apply.py) | Applies grounded windows; calls eager exclusive close |
| [`src/org_memory/workers/handlers/graph_extraction.py`](../src/org_memory/workers/handlers/graph_extraction.py) | Enqueues conflict jobs after extract |
| [`src/org_memory/workers/handlers/conflicts.py`](../src/org_memory/workers/handlers/conflicts.py) | Async exclusivity + deterministic supersede |

### Graph persistence

| Path | Role |
| ---- | ---- |
| [`src/org_memory/db/repositories/graph/claims.py`](../src/org_memory/db/repositories/graph/claims.py) | `supersede_claim(..., valid_to=)`; slot locks |
| [`src/org_memory/db/repositories/graph/writes.py`](../src/org_memory/db/repositories/graph/writes.py) | Relationship supersede; subject claim/edge viewer reads (validity + belief + all-visible ACL in SQL) |
| [`src/org_memory/db/repositories/graph/search.py`](../src/org_memory/db/repositories/graph/search.py) | Hybrid fact candidates with temporal axes + ACL-in-SQL |
| [`src/org_memory/db/repositories/graph/traversal.py`](../src/org_memory/db/repositories/graph/traversal.py) | Path walks with temporal + ACL-in-SQL |

### Read / compose

| Path | Role |
| ---- | ---- |
| [`src/org_memory/services/facts_query.py`](../src/org_memory/services/facts_query.py) | Subject facts + freshness decay |
| [`src/org_memory/services/facts_diff.py`](../src/org_memory/services/facts_diff.py) | Two-snapshot subject diff over `query_subject_facts` |
| [`src/org_memory/services/retrieve_context.py`](../src/org_memory/services/retrieve_context.py) | Compose; applies temporal plan; `fact_diffs` when `range_end` set |
| [`src/org_memory/services/retrieval.py`](../src/org_memory/services/retrieval.py) | Hybrid search; forwards temporal kwargs to fact candidates |
| [`src/org_memory/api/routes_retrieve.py`](../src/org_memory/api/routes_retrieve.py) | HTTP for compose |
| [`src/org_memory/api/routes_facts.py`](../src/org_memory/api/routes_facts.py) | HTTP for facts / paths / `diff_facts` |

### Tests and eval

| Path | Role |
| ---- | ---- |
| [`tests/postgres/test_query_facts_paths.py`](../tests/postgres/test_query_facts_paths.py) | Temporal filters, ACL, supersession-friendly fixtures |
| [`tests/postgres/test_diff_facts_pg.py`](../tests/postgres/test_diff_facts_pg.py) | Snapshot diff HTTP contract |
| [`tests/postgres/test_retrieve_context.py`](../tests/postgres/test_retrieve_context.py) | Compose + ACL |
| [`evals/retrieval/gold_set.json`](../evals/retrieval/gold_set.json) | Retrieval gold set with temporal cases (current, world host+NL, belief host+NL, grain, joint, snapshot diff, multi) |

### Shipped capabilities

| Capability | Home |
| ---------- | ---- |
| `services/temporality/` package | Grounding, grain match, merge on re-evidence, intent, eager-close, diff |
| Structured time fields on extract | Prompt + `grounding.py` |
| Eager exclusive supersession on apply | `eager_close.py` from extraction, structured writers, promotions, glossary seed |
| Re-evidence temporal merge | `merge.py` from `add_claim` / `add_relationship` |
| NL → `TemporalQueryPlan` in compose | `intent.py`; spend-gated `intent_llm.py` on ambiguity |
| `time_grain` on facts | Column in squashed `0001`; ORM aligned |
| Snapshot diff | `diff.py` / `facts_diff.py` / `POST /tools/diff_facts` |
| Passage temporal caps | Point: upper `date_to` / `updated_to`; range: also lower `date_from` / `updated_from` |
| Temporal gold cases | `evals/retrieval/gold_set.json` (current, world, belief, grain, joint, diffs, multi) |

Ledger filters, ontology flags, deterministic ranking, and async conflict
resolution remain the foundation under these capabilities.

---

## 7. Engineering practices and layout

### Practices

- **One concern per module**; small reversible commits  
- **Thin HTTP / thick domain**: routes pass params; services orchestrate;
  repositories own SQL/ACL  
- **Fail closed**: ambiguous axis → explicit ambiguity; vendor errors → 502;
  no silent invented times  
- **Deterministic winners**; LLM only for exclusivity-when-unknown or optional
  intent assist  
- **Single Alembic `0001`**: edit in place when schema changes; ORM stays
  aligned  
- **Affirmative docs**: describe what the system does  
- **No production mocks**  
- **Compose over primitives**: planner feeds existing `as_of` /
  `believed_as_of` / `as_of_grain` knobs  

### Package layout

```text
src/org_memory/services/temporality/
  __init__.py           # light exports (avoid cycles with graph → grain)
  types.py              # TemporalQueryPlan, GroundedInterval, TimeGrain
  grounding.py          # t_ref + extractor fields → GroundedInterval (pure)
  grain.py              # grain expand + as_of match + SQL validity/belief fragments + read statuses
  merge.py              # reconcile temporal fields on re-evidence (fill-only; corrections via supersession)
  intent.py             # query text → TemporalQueryPlan (rules first)
  intent_llm.py         # spend-gated assist; import as leaf module
  eager_close.py        # exclusive slot close after apply
  diff.py               # pure snapshot classify (added/removed/changed)

src/org_memory/services/facts_diff.py     # subject diff over query_subject_facts
src/org_memory/domain/fact_lifecycle.py   # ranking / transitions
```

- **`domain/`** keeps pure lifecycle and ranking.  
- **`services/temporality/`** owns time interpretation, merge, and eager-close
  orchestration that needs graph repositories.  
- **`extraction_apply.py`**, **`structured_writers.py`**, and
  **`promotions.py`** call grounding + eager_close.  
- **`retrieve_context.py`** calls `plan_temporal_query` when axes are omitted;
  on ambiguous plans calls `assist_temporal_query`; when the plan has
  `range_end`, compose also returns `fact_diffs` and derives passage from/to
  caps; point queries apply upper caps only unless the host set filters.  
- **`POST /tools/diff_facts`** is the snapshot-diff primitive (optional
  `as_of_grain`).  
- **Workers/conflicts** remain the async safety net and keep using the same
  ranking helpers.

### Out of pipeline (explicit backlog)

- Narrative multi-hop time and speech-act distinctions beyond structured facts

### Success criteria

- Registry-exclusive slot returns one active value after a successful apply
  transaction.  
- “Title in March” passes with explicit `as_of` and with an equivalent
  planner-derived plan from natural language.  
- Multi-valued relationships stay multi after re-extract.  
- Ambiguous temporal questions surface ambiguity instead of a silent axis.  
- Structured/promote/glossary writers ground and eager-close like extraction.  
- Current reads agree across facts/paths/hybrid (status + validity-now).  
- Live gold scores current, world, belief (host+NL), grain, joint, diffs.  
- Ruff/mypy/unit/postgres green; public URLs unchanged.

---

## 8. Summary

Organizational temporal truth is a **shipped pipeline**:

> **Ground facts in document time → close exclusive losers deterministically →
> plan which clock the question needs → filter every read channel the same way →
> verify with supersession-aware gold cases.**

Org Memory provides the ledger, ontology exclusivity, deterministic ranking,
grounding, eager write-path close, query intent, compose grain/diffs/passage
caps, and temporal gold under `services/temporality/` and the retrieve surface—
without a second product API.
