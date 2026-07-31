"""Derived Worldbuilder profiles over viewer-scoped retrieval and graph facts.

Profiles are synthesized read-only outputs. Categories: person, team, project,
glossary. Every evidence path enforces the viewer's ACL.

The package splits four concerns:

- ``service``: orchestration (resolve subject, gather evidence, synthesize)
- ``resolution``: mapping names to people and entities under viewer ACL
- ``profile_structure``: pure functions that parse, ground, and seed the
  structured profile JSON
- ``read_source``: loading cited documents and graph records under viewer ACL
"""

from org_memory.services.worldbuilder.service import (
    WorldbuilderCategory,
    WorldbuilderService,
)

__all__ = ["WorldbuilderCategory", "WorldbuilderService"]
