"""Embedding carry-over across re-ingest: unchanged chunk text keeps its vector.

Hermetic Postgres tests (DATABASE_URL only, no vendor calls). The repository
matches outgoing embedded child chunks to incoming chunks by content hash and
embedding model, so editing one section re-embeds only that section.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from tests.postgres.helpers import make_doc

pytestmark = pytest.mark.postgres

_EVENT = datetime(2026, 7, 1, tzinfo=UTC)
_MODEL = "test-embedding-model"


def _make_chunk(doc_id: str, workspace_id: str, index: int, text: str, **overrides):
    from org_memory.db.orm import Chunk
    from org_memory.services.ingest import chunk_content_hash

    fields = dict(
        chunk_id=f"{doc_id}#{index}",
        doc_id=doc_id,
        workspace_id=workspace_id,
        chunk_index=index,
        chunk_role="child",
        parent_chunk_id=None,
        text=text,
        content_hash=chunk_content_hash(text),
        embedding=None,
        embedding_model=None,
        source_type="test_doc",
        title="t",
        author_display_name="",
        event_time=_EVENT,
        deep_link="",
        org_visible=True,
        allowed_principals=[],
        deleted=False,
    )
    fields.update(overrides)
    return Chunk(**fields)


def _seed_embedded_doc(session, workspace_id: str, texts: list[str]) -> str:
    """Insert a document whose child chunks are already embedded under _MODEL."""
    # doc_id is a global primary key, so scope it to the per-test workspace.
    doc_id = f"test:carry-{workspace_id}"
    session.add(make_doc(
        doc_id=doc_id,
        workspace_id=workspace_id,
        org_visible=True,
        allowed_principals=[],
        event_time=_EVENT,
    ))
    session.flush()
    for index, text in enumerate(texts):
        session.add(
            _make_chunk(
                doc_id,
                workspace_id,
                index,
                text,
                embedding=[float(index + 1)] * 1536,
                embedding_model=_MODEL,
            )
        )
    session.flush()
    return doc_id


def test_unchanged_chunks_keep_embeddings(hermetic_workspace) -> None:
    from org_memory.db.engine import session_scope
    from org_memory.db.orm import Chunk
    from org_memory.db.repositories import DocumentRepository

    with session_scope() as session:
        doc_id = _seed_embedded_doc(session, hermetic_workspace, ["alpha text", "beta text"])
        incoming = [
            _make_chunk(doc_id, hermetic_workspace, 0, "alpha text"),
            _make_chunk(doc_id, hermetic_workspace, 1, "edited beta text"),
        ]
        carried = DocumentRepository(session).replace_chunks(
            doc_id, incoming, reuse_embeddings_for_model=_MODEL
        )
        assert carried == 1

        rows = {
            c.chunk_index: c
            for c in session.query(Chunk).filter(Chunk.doc_id == doc_id).all()
        }
        assert rows[0].embedding is not None
        assert rows[0].embedding_model == _MODEL
        assert rows[1].embedding is None
        assert rows[1].embedding_model is None


def test_carry_over_survives_reordering(hermetic_workspace) -> None:
    """Matching is by content hash, so a chunk that moved position keeps its vector."""
    from org_memory.db.engine import session_scope
    from org_memory.db.orm import Chunk
    from org_memory.db.repositories import DocumentRepository

    with session_scope() as session:
        doc_id = _seed_embedded_doc(session, hermetic_workspace, ["alpha text", "beta text"])
        incoming = [
            _make_chunk(doc_id, hermetic_workspace, 0, "beta text"),
            _make_chunk(doc_id, hermetic_workspace, 1, "alpha text"),
        ]
        carried = DocumentRepository(session).replace_chunks(
            doc_id, incoming, reuse_embeddings_for_model=_MODEL
        )
        assert carried == 2
        rows = session.query(Chunk).filter(Chunk.doc_id == doc_id).all()
        assert all(c.embedding is not None for c in rows)


def test_no_carry_over_across_models(hermetic_workspace) -> None:
    """A model change re-embeds everything; vectors never cross model boundaries."""
    from org_memory.db.engine import session_scope
    from org_memory.db.orm import Chunk
    from org_memory.db.repositories import DocumentRepository

    with session_scope() as session:
        doc_id = _seed_embedded_doc(session, hermetic_workspace, ["alpha text"])
        incoming = [_make_chunk(doc_id, hermetic_workspace, 0, "alpha text")]
        carried = DocumentRepository(session).replace_chunks(
            doc_id, incoming, reuse_embeddings_for_model="different-model"
        )
        assert carried == 0
        row = session.query(Chunk).filter(Chunk.doc_id == doc_id).one()
        assert row.embedding is None


def test_no_carry_over_when_disabled(hermetic_workspace) -> None:
    from org_memory.db.engine import session_scope
    from org_memory.db.orm import Chunk
    from org_memory.db.repositories import DocumentRepository

    with session_scope() as session:
        doc_id = _seed_embedded_doc(session, hermetic_workspace, ["alpha text"])
        incoming = [_make_chunk(doc_id, hermetic_workspace, 0, "alpha text")]
        carried = DocumentRepository(session).replace_chunks(
            doc_id, incoming, reuse_embeddings_for_model=None
        )
        assert carried == 0
        row = session.query(Chunk).filter(Chunk.doc_id == doc_id).one()
        assert row.embedding is None
