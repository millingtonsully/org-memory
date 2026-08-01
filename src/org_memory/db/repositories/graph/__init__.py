"""Graph repository package: entities, claims, relationships, paths.

Split for maintainability; callers still import ``GraphRepository``.

- ``base``: session/workspace + all-visible evidence ACL
- ``search``: keyword fact candidates for hybrid retrieval
- ``claims``: claim lifecycle and viewer-scoped claim reads
- ``writes``: entity/relationship mutations and subject reads
- ``traversal``: bounded multi-hop ``paths_from``
"""

from __future__ import annotations

from org_memory.db.repositories.graph.claims import GraphClaimsMixin
from org_memory.db.repositories.graph.search import GraphSearchMixin
from org_memory.db.repositories.graph.traversal import GraphTraversalMixin
from org_memory.db.repositories.graph.writes import GraphWritesMixin


class GraphRepository(
    GraphSearchMixin, GraphClaimsMixin, GraphWritesMixin, GraphTraversalMixin
):
    """Entities, relationships, and claims. Workspace-scoped."""


__all__ = ["GraphRepository"]
