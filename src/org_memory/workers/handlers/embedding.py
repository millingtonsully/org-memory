"""Embedding jobs: chunk vectors for search and identity vectors for people."""

from __future__ import annotations

import hashlib

import structlog
from sqlalchemy.orm import Session

from org_memory.core.settings import get_settings
from org_memory.db.engine import session_scope
from org_memory.db.orm import Chunk, Document, Person, utcnow
from org_memory.db.repositories import SpendRepository
from org_memory.ports.embedder import Embedder
from org_memory.workers.handlers._shared import assert_spend_under_hard_limit

logger = structlog.get_logger(__name__)


def handle_embed_chunks(session: Session, payload: dict, embedder: Embedder, heartbeat=None) -> None:
    """Embed the unembedded child chunks of one document.

    Only rows where ``embedding IS NULL`` are touched, so chunks that kept
    their vector through ingest carry-over cost nothing here. The payload's
    ``content_hash`` pins the document text this job was enqueued for: if the
    document changed since (before or during the vendor call), the job fails
    and the newer ingest's job embeds the current text instead.
    """
    doc_id = payload["doc_id"]
    doc = session.get(Document, doc_id)
    if doc is None or doc.deleted:
        return
    expected_hash = payload.get("content_hash")
    current_hash = hashlib.sha256(doc.rendered_text.encode("utf-8")).hexdigest()
    if expected_hash and expected_hash != current_hash:
        raise RuntimeError("embed_chunks stale content_hash")
    chunks = (
        session.query(Chunk)
        .filter(
            Chunk.doc_id == doc_id,
            Chunk.chunk_role == "child",
            Chunk.embedding.is_(None),
            Chunk.deleted == False,  # noqa: E712
        )
        .order_by(Chunk.chunk_index)
        .all()
    )
    if not chunks:
        return
    texts = [c.text for c in chunks]
    chunk_ids = [c.chunk_id for c in chunks]
    estimate = max(len(texts) * 500, 1)
    with session_scope() as spend_session:
        reservation_id = SpendRepository(spend_session).reserve(
            "embed", "embedding", embedder.model_name, estimate
        )
    if heartbeat is not None:
        heartbeat()
    vectors, tokens = embedder.embed_texts(texts)
    if heartbeat is not None:
        heartbeat()
    doc = session.get(Document, doc_id)
    if doc is None or doc.deleted:
        return
    current_hash = hashlib.sha256(doc.rendered_text.encode("utf-8")).hexdigest()
    if expected_hash and expected_hash != current_hash:
        raise RuntimeError("embed_chunks stale content_hash")
    live_chunks = {
        c.chunk_id: c
        for c in session.query(Chunk)
        .filter(Chunk.chunk_id.in_(chunk_ids), Chunk.deleted == False)  # noqa: E712
        .all()
    }
    for chunk_id, text, vector in zip(chunk_ids, texts, vectors, strict=True):
        chunk = live_chunks.get(chunk_id)
        if chunk is None or chunk.text != text:
            raise RuntimeError("embed_chunks chunk text changed during embed")
        chunk.embedding = vector
        chunk.embedding_model = embedder.model_name
        chunk.updated_at = utcnow()
    with session_scope() as spend_session:
        SpendRepository(spend_session).finalize_reservation(reservation_id, tokens)
    logger.info("worker.embedded", doc_id=doc_id, chunks=len(chunks), tokens=tokens)


def handle_refresh_identity_embedding(
    session: Session, payload: dict, embedder: Embedder, heartbeat=None
) -> None:
    """Recompute the identity vector after a person's aliases changed."""
    person_id = payload.get("person_id")
    if not person_id:
        return
    assert_spend_under_hard_limit()
    if heartbeat is not None:
        heartbeat()
    person = session.get(Person, person_id)
    if person is None or person.merged_into_id:
        return
    if person.workspace_id != get_settings().workspace_id:
        return
    from org_memory.services.entity_resolution import EntityResolutionService

    EntityResolutionService(session, embedder).refresh_identity_embedding(person)
    if heartbeat is not None:
        heartbeat()
    person.updated_at = utcnow()
    logger.info("worker.identity_embedding_refreshed", person_id=person.canonical_id)
