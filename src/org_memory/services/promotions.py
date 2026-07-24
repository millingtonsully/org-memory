"""Agent promote: evidence-backed claim + taxonomy proposal for host apply."""

from __future__ import annotations

from sqlalchemy.orm import Session

from org_memory.core.settings import get_settings
from org_memory.db.orm import Claim, TaxonomyProposal
from org_memory.db.repositories import GraphRepository, JobRepository
from org_memory.db.repositories.proposals import TaxonomyProposalRepository
from org_memory.domain.fact_lifecycle import FactStatus
from org_memory.domain.jobs import JobType
from org_memory.domain.models import Principal
from org_memory.domain.proposals import precedence_class_name, precedence_rank
from org_memory.taxonomy_registry import get_taxonomy_registry


class PromotionService:
    def __init__(self, session: Session):
        self._session = session
        self._graph = GraphRepository(session)
        self._proposals = TaxonomyProposalRepository(session)
        self._jobs = JobRepository(session)

    def promote(
        self,
        *,
        principal: Principal,
        om_canonical_id: str,
        subject_type: str,
        taxonomy_key: str,
        field_key: str,
        value: str,
        evidence_doc_ids: list[str],
        host_entity_id: str = "",
        source_kind: str = "",
        source_id: str = "",
    ) -> dict:
        registry = get_taxonomy_registry()
        subject_type = subject_type.strip().lower()
        taxonomy_key = taxonomy_key.strip()
        field_key = field_key.strip()
        value = value.strip()
        if not value:
            raise ValueError("value must be nonempty")
        if not evidence_doc_ids:
            raise ValueError("evidence_doc_ids must be nonempty")

        # Resolve registry predicate by platform binding.
        predicate = None
        pred_def = None
        for pred in registry.predicates.values():
            binding = pred.platform_binding
            if binding is None:
                continue
            if binding.taxonomy_key == taxonomy_key and binding.field_key == field_key:
                predicate = pred.key
                pred_def = pred
                break
        if predicate is None or pred_def is None:
            raise ValueError(
                f"No taxonomy_registry binding for {taxonomy_key}.{field_key}"
            )
        if subject_type not in pred_def.subject_types:
            raise ValueError(
                f"subject_type {subject_type!r} not allowed for predicate {predicate!r}"
            )

        visible = self._graph.visible_evidence_doc_ids(evidence_doc_ids, principal)
        if len(set(visible)) != len(set(evidence_doc_ids)):
            raise ValueError("Every evidence_doc_id must exist and be visible to the viewer.")

        settings = get_settings()
        created_by = f"agent_promote:{principal.principal_id}"
        quotes = [
            {
                "doc_id": doc_id,
                "quote": f"agent_promote:{taxonomy_key}.{field_key}={value}",
            }
            for doc_id in evidence_doc_ids
        ]
        if source_kind and source_id:
            quotes[0]["source"] = {"kind": source_kind, "id": source_id}

        claim = self._graph.add_claim(
            Claim(
                workspace_id=settings.workspace_id,
                subject_type=subject_type,
                subject_id=om_canonical_id.strip(),
                predicate=predicate,
                object_text=value,
                confidence=1.0,
                status=FactStatus.active.value,
                evidence_doc_ids=sorted(set(evidence_doc_ids)),
                evidence_quotes=quotes,
                origin_subject_id=om_canonical_id.strip(),
                created_by=created_by,
                decided_by=f"agent_promote:{principal.principal_id}",
                valid_from=self._graph.latest_evidence_time(evidence_doc_ids),
            )
        )
        if pred_def.mutually_exclusive:
            self._graph.supersede_slot_rivals(
                claim, f"agent_promote:{principal.principal_id}"
            )

        rank = precedence_rank(
            created_by=created_by,
            evidence_count=len(claim.evidence_doc_ids or []),
        )
        proposal = self._proposals.upsert_pending(
            TaxonomyProposal(
                subject_type=claim.subject_type,
                subject_id=claim.subject_id,
                taxonomy_key=taxonomy_key,
                field_key=field_key,
                predicate=predicate,
                value_text=value,
                confidence=claim.confidence,
                evidence_doc_ids=list(claim.evidence_doc_ids or []),
                source_claim_id=claim.claim_id,
                host_entity_id=host_entity_id.strip(),
                precedence_class=precedence_class_name(rank),
                status="pending",
            )
        )
        self._jobs.enqueue(
            JobType.push_taxonomy_proposal_webhook,
            {"proposal_ids": [proposal.proposal_id]},
        )
        return {
            "proposal_id": proposal.proposal_id,
            "claim_id": claim.claim_id,
            "predicate": predicate,
            "status": "pending",
        }
