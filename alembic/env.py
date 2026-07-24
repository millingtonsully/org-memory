"""Alembic environment

Loads DATABASE_URL and applies the single schema revision under
alembic/versions/0001_initial_schema.py. This project does not keep a
multi-revision migration chain.
"""

from __future__ import annotations

import os

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import create_engine

from org_memory.db.orm import Base

load_dotenv()  # real env vars override .env

config = context.config
target_metadata = Base.metadata


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Migrations need only this one variable "
            "(postgresql+psycopg:// scheme); export it or put it in .env."
        )
    if not url.startswith("postgresql+psycopg://"):
        raise RuntimeError(
            "DATABASE_URL must use the psycopg3 scheme 'postgresql+psycopg://' "
            "so migrations run on the same driver as the app. If your host gives "
            "postgresql://..., replace the scheme with postgresql+psycopg://."
        )
    return url


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_database_url(), pool_pre_ping=True)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
