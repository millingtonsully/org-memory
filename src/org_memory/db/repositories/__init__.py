"""SQL repositories package. Import paths stay stable via re-exports."""

from org_memory.db.repositories.audit import AuditRepository
from org_memory.db.repositories.chunks import ChunkSearchRepository
from org_memory.db.repositories.connectors import ConnectorStatusRepository
from org_memory.db.repositories.documents import DocumentRepository, StaleEnvelopeError
from org_memory.db.repositories.graph import GraphRepository
from org_memory.db.repositories.jobs import JobRepository
from org_memory.db.repositories.legal_hold import LegalHoldRepository
from org_memory.db.repositories.merge_decisions import PersonMergeDecisionRepository
from org_memory.db.repositories.people import PersonRepository
from org_memory.db.repositories.procedural import ProceduralMemoryRepository
from org_memory.db.repositories.proposals import TaxonomyProposalRepository
from org_memory.db.repositories.spend import SpendRepository
from org_memory.db.repositories.synthesis import SynthesisTraceRepository
from org_memory.db.repositories.versions import DocumentVersionRepository

__all__ = [
    "AuditRepository",
    "ChunkSearchRepository",
    "ConnectorStatusRepository",
    "DocumentRepository",
    "DocumentVersionRepository",
    "GraphRepository",
    "JobRepository",
    "LegalHoldRepository",
    "PersonMergeDecisionRepository",
    "PersonRepository",
    "ProceduralMemoryRepository",
    "SpendRepository",
    "StaleEnvelopeError",
    "SynthesisTraceRepository",
    "TaxonomyProposalRepository",
]
