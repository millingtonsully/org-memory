# Temporal model

Org Memory tracks two independent time axes for every structured fact (claims
and relationships). One axis records when something was true in the world; the
other records what the system believed and when.

The end-to-end **temporal truth pipeline** (grounding, eager exclusive
supersession, query intent, and codebase mapping) lives in
[`temporal-truth.md`](temporal-truth.md). This file is the **ledger**: axes,
lifecycle, freshness, grains, and indexes.

## The two axes

**World time (validity)** — `valid_from` / `valid_to` on claims and
relationships. These bound when the stated fact held in reality. The interval
is half-open: a fact is valid at time `t` when `valid_from <= t` and
(`valid_to IS NULL` or `valid_to > t`). An open `valid_to` means the world-time
window has not ended yet; it does **not** by itself mean the fact is “current.”
Current reads require `status = active` **and** a validity window that contains
now (see Query surface).

**System time (belief)** — `recorded_at` / `invalidated_at`. These bound when
the service itself held the fact as active. `recorded_at` is set when the fact
lands; `invalidated_at` is set when supersession or retraction retires it.
A fact was believed at time `t` when `recorded_at <= t` and
(`invalidated_at IS NULL` or `invalidated_at > t`).

The axes answer different questions:

- "What was Alice's title in March?" is a world-time question (`as_of`).
- "What did the system report as Alice's title in March, before the correction
  arrived in April?" is a system-time question (`believed_as_of`).

## Time grain

Optional `time_grain` on claims and relationships (`day`, `month`, `quarter`,
`year`, or `unknown`) records how precise the source was. World-time matching
expands a fact's `valid_from` to the start of its grain, and when the query
supplies `as_of_grain` of `month` / `quarter` / `year`, uses overlap with that
calendar bucket (for example, “in March” matches any fact that intersects
March). Host `as_of_grain` is a closed enum on tool contracts; invalid values
fail with 422. Point queries (`as_of_grain` unset or `day`/`unknown`) keep
half-open point containment after fact-side expansion. Read payloads for
`query_facts`, hybrid search facts, and graph cards include the stored
`time_grain` so agents see the precision used for matching.

## Query surface

`POST /tools/query_facts` and `POST /tools/query_paths` accept both points:

- `as_of` filters on the validity window. Superseded facts keep their original
  validity interval, so as-of reads include both `active` and `superseded`
  rows whose window contains the point.
- `believed_as_of` filters on the belief window. When `as_of` is omitted, the
  same instant is also used as the world-time point (day grain unless the host
  sets `as_of_grain`), so the response matches what would have been current and
  believed then. Host `as_of` still wins for the world clock when both are set.
- Omitting both returns currently active facts whose validity window contains
now (grain-expanded; default query grain `day`), matching hybrid
`fact_candidates`.

Subject claim and edge viewer reads (`claims_for_viewer`,
`relationships_for_viewer`) apply the same grain-aware validity, belief, and
all-visible evidence ACL filters in SQL as hybrid `fact_candidates` and
`paths_from`, so private evidence never enters the viewer result set.

Graph person and entity cards (`GET /v1/graph/persons/{canonical_id}`,
`GET /v1/graph/entities/{entity_id}`) accept the same temporal query
parameters and status rules as `query_facts`, and return `time_grain` on
claims and relationships.

`POST /tools/diff_facts` compares two snapshots of the same subject on one
axis: a world pair (`as_of_from` / `as_of_to`) or a belief pair
(`believed_as_of_from` / `believed_as_of_to`). Optional `as_of_grain` applies to
both world snapshots. The response classifies facts as unchanged, added,
removed, or changed (exclusive predicates that swap values).

`retrieve_context` accepts the same single-point parameters plus `as_of_grain`.
When the caller omits both axes, compose derives a temporal plan from the query
text (see [`temporal-truth.md`](temporal-truth.md)). When rules return
ambiguous, compose calls a spend-gated synthesis assist; vendor or spend
failures surface rather than inventing an axis. Explicit timestamps from the
host always win; host `as_of_grain` wins over planner grain. When the plan is a
snapshot pair (`range_end` set), compose also returns `fact_diffs` and derives
passage `date_from`/`date_to` (or belief `updated_from`/`updated_to`) for the
range. Point queries still apply upper passage caps only unless the host set
bounds. `time_expression` from extraction is grounding-only and is not persisted
on claim/relationship rows.
Path responses include `truncated` (more paths than `limit`) and `capped`
(request depth/limit was clamped to hard maxima).

## Lifecycle and supersession

Facts move through explicit states (`domain/fact_lifecycle.py`): proposed,
active, superseded, retracted. Losing values in a mutually exclusive slot are
superseded with:

- `valid_to` set to the winner’s `valid_from` when available (else a sound
  close time),
- `invalidated_at` set to the moment of supersession,
- `superseded_by_*` pointing at the winning fact.

Rows stay for audit and as-of reads. Retraction happens when a fact's last
evidence document is deleted.

Winner selection is deterministic: precedence class, newest supporting
evidence, confidence, evidence count, then stable id. A model may judge
whether a predicate is mutually exclusive when the taxonomy registry does not
say, but it never picks the winning value.

Supersession closes losers with both world-time and belief-time stamps (see
lifecycle above). Registry-exclusive keys also get an eager write-path close
so “current” readers see one winner as soon as an active fact is applied; the
async conflict worker remains the safety net for duplicates, races,
promotions, and registry-unknown exclusivity. That write-path behavior is
part of the temporal truth pipeline — see
[`temporal-truth.md`](temporal-truth.md).

On re-evidence into an existing live row, temporal merge is **fill-only**:
open `valid_from` / `valid_to` may be filled and `time_grain` may upgrade to a
finer grain; existing closed window ends are preserved. Corrections to a wrong
window use supersession or a new object value, not silent overwrite on merge.

## Freshness on the read path

Separately from validity, active facts decay in ranking as they age.
`query_facts` multiplies confidence by an exponential decay over the fact's
`valid_from` (falling back to `recorded_at`), with a configurable half-life
(`FACT_FRESHNESS_HALF_LIFE_DAYS`, per-predicate override
`freshness_half_life_days`) and a floor (`FACT_FRESHNESS_MIN_DECAY`) so old
facts rank lower without disappearing. Passage retrieval applies the same
decay shape to document event times. Freshness ranking is not a substitute
for supersession.

## Indexes

Schema `0001` creates temporal indexes so `query_facts` / `query_paths` can
filter by subject (or edge endpoint) and contain a point in either time axis
without scanning the full table:

- B-tree: `ix_claims_subject_status`, `ix_relationships_from_status`
  (workspace + endpoint + status).
- Unique live object: `uq_claims_live_object` on
  `(workspace_id, subject_type, subject_id, predicate, object_text)` where
  status is `proposed`, `active`, or `retracted` (superseded rows remain for
  audit). `add_claim` collapses same-object live rows and merges on unique
  races.
- GiST ranges (requires `btree_gist`): `ix_claims_subject_valid_range`,
  `ix_claims_subject_belief_range`, and the matching
  `ix_relationships_from_*_range` indexes on
  `tstzrange(valid_from, valid_to)` and
  `tstzrange(recorded_at, invalidated_at)`.

Because this project keeps a single squashed Alembic revision, already-migrated
databases must be reset or recreated so `alembic upgrade head` applies the
new indexes and columns. Fresh CI and Docker databases pick them up
automatically.

## Documents and chunks

Documents carry `event_time` (when the content happened in the source system)
and ACL event times used for last-writer-wins conflict resolution on
permission changes. Stale envelopes, detected by comparing event times, are
rejected rather than applied out of order. Ingress rejects naive
`event_time` values, years before 1990 (common unset/default clocks), and
timestamps more than one day in the future. Document `event_time` is the
reference clock (`t_ref`) for grounding extracted fact windows. Chunk-level
`content_hash` values let re-ingest keep embeddings for text that did not
change, so temporal churn in metadata never forces re-embedding.
