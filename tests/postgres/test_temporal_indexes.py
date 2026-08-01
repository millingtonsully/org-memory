"""Sanity-check temporal indexes from schema 0001.

Requires a database migrated with the current squashed 0001 (fresh
`alembic upgrade head`). Already-stamped DBs without a recreate will fail
these assertions until reset.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.postgres

_EXPECTED = (
    "ix_claims_subject_status",
    "ix_claims_subject_valid_range",
    "ix_claims_subject_belief_range",
    "ix_relationships_from_status",
    "ix_relationships_from_valid_range",
    "ix_relationships_from_belief_range",
)


def test_temporal_indexes_exist(hermetic_workspace) -> None:
    from org_memory.db.engine import session_scope

    with session_scope() as session:
        rows = session.execute(
            text(
                """
                SELECT indexname
                FROM pg_indexes
                WHERE schemaname = current_schema()
                  AND indexname IN (
                    'ix_claims_subject_status',
                    'ix_claims_subject_valid_range',
                    'ix_claims_subject_belief_range',
                    'ix_relationships_from_status',
                    'ix_relationships_from_valid_range',
                    'ix_relationships_from_belief_range'
                  )
                """
            )
        ).fetchall()

    found = {r[0] for r in rows}
    missing = sorted(set(_EXPECTED) - found)
    assert not missing, (
        "Temporal indexes missing from the live DB. Recreate the database and "
        f"run `alembic upgrade head`. Missing: {missing}"
    )


def test_btree_gist_extension_available(hermetic_workspace) -> None:
    from org_memory.db.engine import session_scope

    with session_scope() as session:
        installed = session.execute(
            text("SELECT 1 FROM pg_extension WHERE extname = 'btree_gist'")
        ).scalar()
    assert installed == 1, "btree_gist extension required for temporal GiST indexes"
