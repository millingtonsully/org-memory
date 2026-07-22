"""Aggregate collaboration_edges from document_participants."""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import UTC, datetime
from itertools import combinations

import structlog
from sqlalchemy.orm import Session

from org_memory.core.settings import get_settings
from org_memory.db.orm import CollaborationEdge, Document, DocumentParticipant, utcnow
from org_memory.db.repositories import GraphRepository
from org_memory.domain.models import Principal

logger = structlog.get_logger(__name__)

_ROLE_WEIGHT = {
    "author": 1.0,
    "to": 0.9,
    "cc": 0.5,
    "participant": 0.8,
    "reviewer": 0.7,
}
_ORG_VISIBLE_PENALTY = 0.25  # broadcast/mass channels contribute less
_HALF_LIFE_DAYS = 90.0


def _ordered_pair(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a < b else (b, a)


def _recency_weight(event_time: datetime, now: datetime) -> float:
    if event_time.tzinfo is None:
        event_time = event_time.replace(tzinfo=UTC)
    age_days = max(0.0, (now - event_time).total_seconds() / 86400.0)
    return math.pow(0.5, age_days / _HALF_LIFE_DAYS)


class CollaborationService:
    def __init__(self, session: Session):
        self._session = session
        self._ws = get_settings().workspace_id
        self._graph = GraphRepository(session)

    def rebuild_edges(self) -> dict:
        """Full recompute of undirected co_participant edges for the workspace."""
        now = utcnow()
        # person_id -> list of (doc_id, role, org_visible, event_time)
        participants = (
            self._session.query(DocumentParticipant, Document)
            .join(Document, Document.doc_id == DocumentParticipant.doc_id)
            .filter(
                DocumentParticipant.workspace_id == self._ws,
                Document.workspace_id == self._ws,
                Document.deleted == False,  # noqa: E712
                DocumentParticipant.person_id.isnot(None),
                DocumentParticipant.identity_kind != "service",
            )
            .all()
        )

        by_doc: dict[str, list[tuple[str, str, bool, datetime]]] = defaultdict(list)
        for part, doc in participants:
            assert part.person_id is not None
            by_doc[doc.doc_id].append(
                (part.person_id, part.role, doc.org_visible, doc.event_time)
            )

        # pair -> weight, evidence, last_seen
        acc: dict[tuple[str, str], dict] = {}
        for doc_id, people in by_doc.items():
            # Unique people on this doc
            unique: dict[str, tuple[str, bool, datetime]] = {}
            for person_id, role, org_visible, event_time in people:
                prev = unique.get(person_id)
                if prev is None or _ROLE_WEIGHT.get(role, 0.6) > _ROLE_WEIGHT.get(prev[0], 0.6):
                    unique[person_id] = (role, org_visible, event_time)
            if len(unique) < 2:
                continue
            org_visible = next(iter(unique.values()))[1]
            event_time = max(v[2] for v in unique.values())
            base = _ORG_VISIBLE_PENALTY if org_visible else 1.0
            base *= _recency_weight(event_time, now)
            for a, b in combinations(sorted(unique), 2):
                role_a = _ROLE_WEIGHT.get(unique[a][0], 0.6)
                role_b = _ROLE_WEIGHT.get(unique[b][0], 0.6)
                delta = base * math.sqrt(role_a * role_b)
                key = _ordered_pair(a, b)
                slot = acc.setdefault(
                    key,
                    {"weight": 0.0, "docs": set(), "last_seen": event_time},
                )
                slot["weight"] += delta
                slot["docs"].add(doc_id)
                if event_time > (slot["last_seen"] or event_time):
                    slot["last_seen"] = event_time

        # Replace undirected co_participant edges for this workspace.
        self._session.query(CollaborationEdge).filter(
            CollaborationEdge.workspace_id == self._ws,
            CollaborationEdge.edge_type == "co_participant",
            CollaborationEdge.directed == False,  # noqa: E712
        ).delete(synchronize_session=False)

        written = 0
        for (a, b), slot in acc.items():
            self._session.add(
                CollaborationEdge(
                    workspace_id=self._ws,
                    person_a_id=a,
                    person_b_id=b,
                    edge_type="co_participant",
                    weight=float(slot["weight"]),
                    evidence_doc_ids=sorted(slot["docs"]),
                    last_seen_at=slot["last_seen"],
                    directed=False,
                )
            )
            written += 1
        self._session.flush()
        summary = {"docs_considered": len(by_doc), "edges": written}
        logger.info("collaboration.rebuilt", **summary)
        return summary

    def top_collaborators(
        self,
        person_id: str,
        principal: Principal,
        *,
        limit: int = 20,
    ) -> list[dict]:
        edges = (
            self._session.query(CollaborationEdge)
            .filter(
                CollaborationEdge.workspace_id == self._ws,
                CollaborationEdge.edge_type == "co_participant",
                (
                    (CollaborationEdge.person_a_id == person_id)
                    | (CollaborationEdge.person_b_id == person_id)
                ),
            )
            .order_by(CollaborationEdge.weight.desc())
            .limit(limit * 3)  # ACL filter may drop some
            .all()
        )
        results: list[dict] = []
        for edge in edges:
            evidence = list(edge.evidence_doc_ids or [])
            if not evidence:
                continue
            visible = self._graph.visible_evidence_doc_ids(evidence, principal)
            if len(visible) != len(set(evidence)):
                continue
            other = edge.person_b_id if edge.person_a_id == person_id else edge.person_a_id
            results.append(
                {
                    "person_id": other,
                    "weight": edge.weight,
                    "edge_type": edge.edge_type,
                    "evidence_doc_ids": visible,
                    "last_seen_at": edge.last_seen_at.isoformat() if edge.last_seen_at else None,
                }
            )
            if len(results) >= limit:
                break
        return results

    def pair_strength(
        self,
        person_a: str,
        person_b: str,
        principal: Principal,
    ) -> dict | None:
        a, b = _ordered_pair(person_a, person_b)
        edge = (
            self._session.query(CollaborationEdge)
            .filter(
                CollaborationEdge.workspace_id == self._ws,
                CollaborationEdge.person_a_id == a,
                CollaborationEdge.person_b_id == b,
                CollaborationEdge.edge_type == "co_participant",
            )
            .one_or_none()
        )
        if edge is None:
            return None
        evidence = list(edge.evidence_doc_ids or [])
        visible = self._graph.visible_evidence_doc_ids(evidence, principal)
        if len(visible) != len(set(evidence)):
            return None
        return {
            "person_a_id": a,
            "person_b_id": b,
            "weight": edge.weight,
            "edge_type": edge.edge_type,
            "evidence_doc_ids": visible,
            "last_seen_at": edge.last_seen_at.isoformat() if edge.last_seen_at else None,
        }
