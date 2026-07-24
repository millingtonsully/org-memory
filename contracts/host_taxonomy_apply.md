# Host taxonomy apply contract

Org Memory emits taxonomy proposals. The **host platform** commits ontology
field updates. Org Memory does not run workflows, automations, or entity LWW.

## Pull / push

- Pull: `GET /v1/taxonomy-proposals`
- Push: optional webhook when `TAXONOMY_PROPOSAL_WEBHOOK_URL` is set.
  When `TAXONOMY_PROPOSAL_WEBHOOK_SECRET` is set, requests include
  `X-Org-Memory-Signature: sha256=<hex>` over the exact JSON body.
- Agent promote: `POST /v1/promotions` creates an OM claim and a pending proposal

## Proposal payload fields

| Field | Meaning |
|-------|---------|
| `proposal_id` | OM proposal id |
| `subject_type` / `subject_id` | OM canonical subject (e.g. person id) |
| `host_entity_id` | Optional host entity id when known (promote path) |
| `taxonomy_key` / `field_key` | Host ontology binding |
| `predicate` | Registry predicate key |
| `value` | Proposed field value |
| `confidence` | Claim confidence |
| `evidence_doc_ids` | OM document ids supporting the value |
| `source_claim_id` | OM claim id |
| `precedence_class` | `ground_truth` / `agent_promote` / `extraction_multi` / `extraction_single` |
| `status` | `pending` until host callbacks |

## Host apply steps

1. Map `subject_id` (and `host_entity_id` when present) to the host entity.
2. Update the host field (`taxonomy_key.field_key`) with `value`.
3. Persist `evidence_doc_ids` with the field update for audit.
4. Callback `POST /v1/taxonomy-proposals/{id}/applied` or `/rejected` with `decided_by`.

## Idempotency

- One pending proposal per `(subject, taxonomy_key, field_key)` slot.
- Re-promote / regenerate supersedes the prior pending row.
- Host should treat apply as idempotent on `(proposal_id)` or slot + value.

## Non-goals

- Org Memory does not create host workflows, tasks, SMS, or automations.
- Org Memory is not the LWW store for host ontology.
