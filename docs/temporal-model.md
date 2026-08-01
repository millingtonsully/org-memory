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

## Time grain

Optional `time_grain` on claims and relationships (`day`, `month`, `quarter`,
`year`, or `unknown`) records how precise the source was. World-time matching
respects grain (for example, “in March” uses month containment) so the ledger
does not pretend day-level precision the evidence never had.

## Query surface

`POST /tools/query_facts` and `POST /tools/query_paths` accept both points:

- `as_of` filters on the validity window. Superseded facts keep their original
  validity interval, so as-of reads include both `active` and `superseded`
  rows whose window contains the point.
- `believed_as_of` filters on the belief window, reconstructing what the
  service would have returned at that moment.
- Omitting both returns currently active facts with open validity.

`POST /tools/diff_facts` compares two snapshots of the same subject on one
axis: a world pair (`as_of_from` / `as_of_to`) or a belief pair
(`believed_as_of_from` / `believed_as_of_to`). The response classifies facts as
unchanged, added, removed, or changed (exclusive predicates that swap values).

`retrieve_context` accepts the same single-point parameters. When the caller
omits both axes, compose derives a temporal plan from the query text (see
[`temporal-truth.md`](temporal-truth.md)). Explicit timestamps from the host
always win. When the plan is a snapshot pair (`range_end` set), compose also
returns `fact_diffs`. Path responses include `truncated` (more paths than
`limit`) and `capped` (request depth/limit was clamped to hard maxima).

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
rejected rather than applied out of order. Document `event_time` is the
reference clock (`t_ref`) for grounding extracted fact windows. Chunk-level
`content_hash` values let re-ingest keep embeddings for text that did not
change, so temporal churn in metadata never forces re-embedding.
