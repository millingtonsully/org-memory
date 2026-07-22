"""SQLAlchemy ORM models for Postgres.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Active embedding size (text-embedding-3-small). Other dimensions need a new
# column and re-embed migration.
EMBEDDING_DIM = 1536


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


class Document(Base):
    __tablename__ = "documents"

    # doc_id = "{source_system}:{external_id}"
    doc_id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String, nullable=False)
    source_system: Mapped[str] = mapped_column(String, nullable=False)
    external_id: Mapped[str] = mapped_column(String, nullable=False)
    source_type: Mapped[str] = mapped_column(String, nullable=False)

    title: Mapped[str] = mapped_column(Text, default="")
    rendered_text: Mapped[str] = mapped_column(Text, default="")

    author_external_id: Mapped[str] = mapped_column(String, default="")
    author_display_name: Mapped[str] = mapped_column(String, default="")
    author_email: Mapped[str] = mapped_column(String, default="")

    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    org_visible: Mapped[bool] = mapped_column(Boolean, default=False)
    allowed_principals: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    acl_version: Mapped[int] = mapped_column(Integer, default=1)
    # Independent of content event_time so stale permission_change envelopes
    # cannot reopen access after a newer ACL restriction.
    acl_event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    parent_external_id: Mapped[str] = mapped_column(String, default="")
    deep_link: Mapped[str] = mapped_column(String, default="")
    doc_metadata: Mapped[dict] = mapped_column(JSONB, default=dict)

    raw_blob_key: Mapped[str] = mapped_column(String, default="")

    deleted: Mapped[bool] = mapped_column(Boolean, default=False)


class Chunk(Base):
    __tablename__ = "chunks"

    chunk_id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    doc_id: Mapped[str] = mapped_column(String, ForeignKey("documents.doc_id"), nullable=False)
    workspace_id: Mapped[str] = mapped_column(String, nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)

    text: Mapped[str] = mapped_column(Text, nullable=False)

    # Filled by the embed worker; vector search filters on model name
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM), nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String, nullable=True)

    # Denormalized from document for single-table search
    source_type: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str] = mapped_column(Text, default="")
    author_display_name: Mapped[str] = mapped_column(String, default="")
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    deep_link: Mapped[str] = mapped_column(String, default="")
    org_visible: Mapped[bool] = mapped_column(Boolean, default=False)
    allowed_principals: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)

    # text_search is a GENERATED tsvector column (see alembic initial schema)


class Person(Base):
    """Canonical person record. Only resolution and merges write here."""

    __tablename__ = "persons"

    canonical_id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(String, nullable=False)
    display_name: Mapped[str] = mapped_column(String, nullable=False)
    name_aliases: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    primary_email: Mapped[str] = mapped_column(String, default="")
    resolution_status: Mapped[str] = mapped_column(String, default="provisional")
    # Derived only from observed aliases; never LLM-invented profile data.
    identity_metadata: Mapped[dict] = mapped_column(JSONB, default=dict)
    merged_into_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("persons.canonical_id"), nullable=True
    )
    # Semantic identity descriptors generate merge CANDIDATES only. A vector
    # match never authorizes a merge.
    identity_embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM), nullable=True)
    identity_embedding_model: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Entity(Base):
    """Non-person entity (team, project, etc.)."""

    __tablename__ = "entities"

    entity_id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(String, nullable=False)
    entity_type: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    normalized_name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    attributes: Mapped[dict] = mapped_column(JSONB, default=dict)
    resolution_status: Mapped[str] = mapped_column(String, default="provisional")
    # Provenance for viewer scoping. An entity is returned only when *every*
    # cited document is visible to the requesting principal (all-visible).
    evidence_doc_ids: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Relationship(Base):
    """Typed graph edge with validity window, evidence, and status lifecycle."""

    __tablename__ = "relationships"

    relationship_id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(String, nullable=False)
    from_type: Mapped[str] = mapped_column(String, nullable=False)
    from_id: Mapped[str] = mapped_column(String, nullable=False)
    to_type: Mapped[str] = mapped_column(String, nullable=False)
    to_id: Mapped[str] = mapped_column(String, nullable=False)
    from_label: Mapped[str] = mapped_column(String, default="")
    to_label: Mapped[str] = mapped_column(String, default="")
    relationship_type: Mapped[str] = mapped_column(String, nullable=False)
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    status: Mapped[str] = mapped_column(String, default="proposed")
    evidence_doc_ids: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    evidence_quotes: Mapped[list[dict]] = mapped_column(JSONB, default=list)
    origin_from_id: Mapped[str] = mapped_column(String, default="")
    origin_to_id: Mapped[str] = mapped_column(String, default="")
    superseded_by_relationship_id: Mapped[str] = mapped_column(String, default="")
    created_by: Mapped[str] = mapped_column(String, nullable=False)
    decided_by: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Claim(Base):
    """Extracted statement about a subject."""

    __tablename__ = "claims"

    claim_id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(String, nullable=False)
    subject_type: Mapped[str] = mapped_column(String, nullable=False)
    subject_id: Mapped[str] = mapped_column(String, nullable=False)
    predicate: Mapped[str] = mapped_column(String, nullable=False)
    object_text: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    status: Mapped[str] = mapped_column(String, default="proposed")
    evidence_doc_ids: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    evidence_quotes: Mapped[list[dict]] = mapped_column(JSONB, default=list)
    origin_subject_id: Mapped[str] = mapped_column(String, default="")
    superseded_by_claim_id: Mapped[str] = mapped_column(String, default="")
    created_by: Mapped[str] = mapped_column(String, nullable=False)
    decided_by: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PersonMergeDecision(Base):
    """Append-only machine decision for a candidate pair and any reversal."""

    __tablename__ = "person_merge_decisions"

    decision_id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(String, nullable=False)
    subject_kind: Mapped[str] = mapped_column(String, nullable=False)
    a_id: Mapped[str] = mapped_column(String, nullable=False)
    b_id: Mapped[str] = mapped_column(String, nullable=False)
    verdict: Mapped[str] = mapped_column(String, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    reason: Mapped[str] = mapped_column(Text, default="")
    signals: Mapped[list[str]] = mapped_column(JSONB, default=list)
    evidence_fingerprint: Mapped[str] = mapped_column(String, nullable=False)
    # auto_merged | different | unsure | blocked_conflict | split_conflict
    status: Mapped[str] = mapped_column(String, nullable=False)
    decided_by: Mapped[str] = mapped_column(String, default="")
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reversed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reversal_reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DocumentVersion(Base):
    """One accepted envelope version, pointing at an archived blob."""

    __tablename__ = "document_versions"

    version_id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    doc_id: Mapped[str] = mapped_column(String, ForeignKey("documents.doc_id"), nullable=False)
    workspace_id: Mapped[str] = mapped_column(String, nullable=False)
    change_kind: Mapped[str] = mapped_column(String, nullable=False)
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    blob_key: Mapped[str] = mapped_column(String, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LegalHold(Base):
    """Retention exemption for a doc, source, or person until released."""

    __tablename__ = "legal_holds"

    hold_id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(String, nullable=False)
    scope_type: Mapped[str] = mapped_column(String, nullable=False)
    scope_value: Mapped[str] = mapped_column(String, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    placed_by: Mapped[str] = mapped_column(String, nullable=False)
    placed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    released_by: Mapped[str] = mapped_column(String, default="")
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ConnectorStatus(Base):
    """Per-source ingest freshness and failure counters."""

    __tablename__ = "connector_status"

    workspace_id: Mapped[str] = mapped_column(String, primary_key=True)
    source_system: Mapped[str] = mapped_column(String, primary_key=True)
    last_envelope_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_event_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    envelopes_total: Mapped[int] = mapped_column(BigInteger, default=0)
    failures_total: Mapped[int] = mapped_column(BigInteger, default=0)
    last_error: Mapped[str] = mapped_column(Text, default="")
    last_failure_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    recent_errors: Mapped[list] = mapped_column(JSONB, default=list)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SynthesisTrace(Base):
    """Audit row for LLM synthesis inputs, output, and token cost."""

    __tablename__ = "synthesis_traces"

    trace_id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(String, nullable=False)
    principal_id: Mapped[str] = mapped_column(String, nullable=False)
    tool: Mapped[str] = mapped_column(String, nullable=False)
    subject: Mapped[str] = mapped_column(String, default="")
    model: Mapped[str] = mapped_column(String, nullable=False)
    input_doc_ids: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    output_text: Mapped[str] = mapped_column(Text, nullable=False)
    tokens: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PersonAlias(Base):
    """Maps source identity to a canonical person."""

    __tablename__ = "person_aliases"

    alias_id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    person_id: Mapped[str] = mapped_column(String, ForeignKey("persons.canonical_id"), nullable=False)
    # Immutable owner before canonical merges; enables deterministic auto-split.
    observed_person_id: Mapped[str] = mapped_column(
        String, ForeignKey("persons.canonical_id"), nullable=False
    )
    workspace_id: Mapped[str] = mapped_column(String, nullable=False)
    source_system: Mapped[str] = mapped_column(String, nullable=False)
    external_id: Mapped[str] = mapped_column(String, default="")
    display_name: Mapped[str] = mapped_column(String, default="")
    email: Mapped[str] = mapped_column(String, default="")
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DocumentParticipant(Base):
    """A source-observed actor role, resolved to Person only when warranted."""

    __tablename__ = "document_participants"

    participant_id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    doc_id: Mapped[str] = mapped_column(String, ForeignKey("documents.doc_id"), nullable=False)
    workspace_id: Mapped[str] = mapped_column(String, nullable=False)
    role: Mapped[str] = mapped_column(String, nullable=False)
    identity_kind: Mapped[str] = mapped_column(String, nullable=False)
    source_system: Mapped[str] = mapped_column(String, nullable=False)
    external_id: Mapped[str] = mapped_column(String, default="")
    display_name: Mapped[str] = mapped_column(String, default="")
    emails: Mapped[list[dict]] = mapped_column(JSONB, default=list)
    identifiers: Mapped[list[dict]] = mapped_column(JSONB, default=list)
    person_id: Mapped[str | None] = mapped_column(String, ForeignKey("persons.canonical_id"), nullable=True)
    observed_person_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("persons.canonical_id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ExtractionWindow(Base):
    """Durable parsed LLM output so retries do not pay for completed windows."""

    __tablename__ = "extraction_windows"

    doc_id: Mapped[str] = mapped_column(String, ForeignKey("documents.doc_id"), primary_key=True)
    content_hash: Mapped[str] = mapped_column(String, primary_key=True)
    window_index: Mapped[int] = mapped_column(Integer, primary_key=True)
    window_hash: Mapped[str] = mapped_column(String, nullable=False)
    parsed_output: Mapped[dict] = mapped_column(JSONB, nullable=False)
    tokens: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ProceduralMemory(Base):
    """Searchable agent episode; source events remain intact for audit."""

    __tablename__ = "procedural_memories"

    memory_id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(String, nullable=False)
    agent_id: Mapped[str] = mapped_column(String, nullable=False)
    run_id: Mapped[str] = mapped_column(String, nullable=False)
    procedure_key: Mapped[str] = mapped_column(String, nullable=False)
    objective: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    events: Mapped[list[dict]] = mapped_column(JSONB, nullable=False)
    raw_synthesis: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String, default="active")
    superseded_by_memory_id: Mapped[str] = mapped_column(String, default="")
    evidence_doc_ids: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    org_visible: Mapped[bool] = mapped_column(Boolean, default=False)
    allowed_principals: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    synthesis_model: Mapped[str] = mapped_column(String, nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM), nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String, nullable=True)
    created_by_principal: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CollaborationEdge(Base):
    """Aggregated who-works-with-whom edge from document participants."""

    __tablename__ = "collaboration_edges"

    edge_id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(String, nullable=False)
    person_a_id: Mapped[str] = mapped_column(String, nullable=False)
    person_b_id: Mapped[str] = mapped_column(String, nullable=False)
    edge_type: Mapped[str] = mapped_column(String, default="co_participant")
    weight: Mapped[float] = mapped_column(Float, default=0.0)
    evidence_doc_ids: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    directed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TaxonomyProposal(Base):
    """Auto write-back candidate for a caller-platform taxonomy field (no HITL)."""

    __tablename__ = "taxonomy_proposals"

    proposal_id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(String, nullable=False)
    subject_type: Mapped[str] = mapped_column(String, nullable=False)
    subject_id: Mapped[str] = mapped_column(String, nullable=False)
    taxonomy_key: Mapped[str] = mapped_column(String, nullable=False)
    field_key: Mapped[str] = mapped_column(String, nullable=False)
    predicate: Mapped[str] = mapped_column(String, nullable=False)
    value_text: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    evidence_doc_ids: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    source_claim_id: Mapped[str] = mapped_column(String, default="")
    # ground_truth | extraction_multi | extraction_single
    precedence_class: Mapped[str] = mapped_column(String, default="extraction_single")
    # pending | applied | rejected | superseded
    status: Mapped[str] = mapped_column(String, default="pending")
    superseded_by_id: Mapped[str] = mapped_column(String, default="")
    decided_by: Mapped[str] = mapped_column(String, default="")
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_push_error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Job(Base):
    """Durable work queue with leases and retries."""

    __tablename__ = "jobs"

    job_id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(String, nullable=False)
    job_type: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    last_error: Mapped[str] = mapped_column(Text, default="")
    raw_error: Mapped[str] = mapped_column(Text, default="")
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SpendEntry(Base):
    """Token spend row for metering (no hard budget cap)."""

    __tablename__ = "spend_ledger"

    entry_id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(String, nullable=False)
    job_class: Mapped[str] = mapped_column(String, nullable=False)
    vendor: Mapped[str] = mapped_column(String, nullable=False)
    model: Mapped[str] = mapped_column(String, nullable=False)
    tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RetrievalAudit(Base):
    """Log of who searched what and which chunks were returned."""

    __tablename__ = "retrieval_audits"

    audit_id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(String, nullable=False)
    principal_id: Mapped[str] = mapped_column(String, nullable=False)
    tool: Mapped[str] = mapped_column(String, nullable=False)
    query: Mapped[str] = mapped_column(Text, nullable=False)
    params: Mapped[dict] = mapped_column(JSONB, default=dict)
    result_chunk_ids: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    result_fact_ids: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    result_memory_ids: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AdminAudit(Base):
    """Append-only log of mutating admin actions (legal hold, retention, jobs)."""

    __tablename__ = "admin_audits"

    audit_id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(String, nullable=False)
    principal_id: Mapped[str] = mapped_column(String, nullable=False)
    action: Mapped[str] = mapped_column(String, nullable=False)
    params: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
