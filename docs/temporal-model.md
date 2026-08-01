# Temporal model

Org Memory tracks two independent time axes for every structured fact (claims
and relationships). This is the standard bi-temporal design: one axis records
when something was true in the world, the other records what the system
believed and when.

## The two axes

**World time (validity)** — `valid_from` / `valid_to` on claims and
relationships. These bound when the stated fact held in reality. The interval
is half-open: a fact is valid at time `t` when `valid_from <= t` and
(`valid_to IS NULL` or `valid_to > t`). An open `valid_to` means the fact is
still current as far as the system knows.

**System time (belief)** — `recorded_at` / `invalidated_at`. These bound when
the service itself held the fact as active. `recorded_at` is set when the fact
lands; `invalidated_at` is set when supersession or retraction retires it.
A fact was believed at time `t` when `recorded_at <= t` and
(`invalidated_at IS NULL` or `invalidated_at > t`).

The axes answer different questions:

- "What was Alice's title in March?" is a world-time question (`as_of`).
- "What did the system report as Alice's title in March, before the correction
  arrived in April?" is a system-time question (`believed_as_of`).

## Query surface

`POST /tools/query_facts` accepts both points:

- `as_of` filters on the validity window. Because a superseded fact keeps its
  original validity interval, as-of reads include both `active` and
  `superseded` claims whose window contains the point.
- `believed_as_of` filters on the belief window, reconstructing what the
  service would have returned at that moment.
- Omitting both returns currently active facts with open validity.

`POST /tools/query_paths` accepts both points on relationship edges:

- `as_of` filters on each edge's validity window (active and superseded).
- `believed_as_of` filters on each edge's belief window.
- Responses include `truncated` (more paths than `limit`) and `capped`
  (request depth/limit was clamped to hard maxima).

## Lifecycle and supersession

Facts move through explicit states (`domain/fact_lifecycle.py`): proposed,
active, superseded, retracted. Losing values in a mutually exclusive slot are
superseded with a pointer to the winning fact and an `invalidated_at`
timestamp. They stay in the database for audit and as-of reads. Retraction
happens when a fact's last evidence document is deleted; retracted facts leave
the active read path the same way.

Winner selection is deterministic: precedence class, newest supporting
evidence, confidence, evidence count, then stable id. A model may judge
whether a predicate is mutually exclusive when the taxonomy registry does not
say, but it never picks the winning value.

## Freshness on the read path

Separately from validity, active facts decay in ranking as they age.
`query_facts` multiplies confidence by an exponential decay over the fact's
`valid_from` (falling back to `recorded_at`), with a configurable half-life
(`FACT_FRESHNESS_HALF_LIFE_DAYS`, per-predicate override
`freshness_half_life_days`) and a floor (`FACT_FRESHNESS_MIN_DECAY`) so old
facts rank lower without disappearing. Passage retrieval applies the same
decay shape to document event times.

## Documents and chunks

Documents carry `event_time` (when the content happened in the source system)
and ACL event times used for last-writer-wins conflict resolution on
permission changes. Stale envelopes, detected by comparing event times, are
rejected rather than applied out of order. Chunk-level `content_hash` values
let re-ingest keep embeddings for text that did not change, so temporal churn
in metadata never forces re-embedding.
