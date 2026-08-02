"""Graph repository package: entities, claims, relationships, paths.

Split for maintainability; callers still import ``GraphRepository``.

- ``base``: session/workspace + all-visible evidence ACL SQL fragments
- ``search``: keyword fact candidates for hybrid retrieval
- ``claims``: claim lifecycle and viewer-scoped claim reads
- ``entities``: entity mutations and viewer browse/search
- ``relationships``: relationship lifecycle and viewer-scoped edge reads
- ``evidence``: document-scoped evidence retraction
- ``traversal``: bounded multi-hop ``paths_from``
"""

from __future__ import annotations

from org_memory.db.repositories.graph.claims import GraphClaimsMixin
from org_memory.db.repositories.graph.entities import GraphEntitiesMixin
from org_memory.db.repositories.graph.evidence import GraphEvidenceMixin
from org_memory.db.repositories.graph.relationships import GraphRelationshipsMixin
from org_memory.db.repositories.graph.search import GraphSearchMixin
from org_memory.db.repositories.graph.traversal import GraphTraversalMixin


class GraphRepository(
    GraphSearchMixin,
    GraphClaimsMixin,
    GraphEntitiesMixin,
    GraphRelationshipsMixin,
    GraphEvidenceMixin,
    GraphTraversalMixin,
):
    """Entities, relationships, and claims. Workspace-scoped."""


__all__ = ["GraphRepository"]
